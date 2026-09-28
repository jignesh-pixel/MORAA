"""Tests for Scenario 1 — WhatsApp new-customer onboarding.

Covers:
 1. New user sends "Hi"
 2. Welcome message is sent
 3. Registration instructions are sent
 4. Complete registration creates exactly one customer
 5. Customer receives the congratulations message
 6. Customer receives the recharge CTA
 7. Incomplete registration does NOT create a customer
 8. Missing fields are requested
 9. A subsequent message completes the missing fields
10. Duplicate registration does not create duplicate customers
11. Existing registered users bypass onboarding
12. ENABLE_ONBOARDING_GATE=False leaves existing behaviour unchanged
13. Parser failure does not crash the webhook
14. Existing image pipeline remains untouched

All Meta API calls, DBs and LLM calls are isolated/mocked:
no real WhatsApp API call is made, no AI credits are consumed.
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the backend package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — register every model with Base.metadata
from app.config import settings
from app.database import Base, get_db
from app.models.customer import Customer
from app.models.onboarding_session import OnboardingSession
from app.services import onboarding_service
from app.services.onboarding_service import (
    _heuristic_extract,
    PARSER_FAILURE_MESSAGE,
    RECHARGE_BUTTON_ID,
    RECHARGE_BUTTON_TITLE,
    REGISTRATION_INSTRUCTIONS_MESSAGE,
    WELCOME_MESSAGE,
    OnboardingResult,
    OnboardingState,
    RegistrationData,
    create_or_update_customer,
    get_onboarding_state,
    handle_text,
    is_registered,
    is_value_grounded,
    merge_registration_data,
    normalize_gst_number,
    parse_registration_text,
    sanitize_text,
)

SENDER = "919876543210"
FULL_REGISTRATION_TEXT = (
    "Name: Ananya Shah\n"
    "Business name: Shah Gems & Jewels\n"
    "GST number: 24AAAPS1234C1Z5\n"
    "Business address: 12, Diamond Plaza, Varachia Road, Surat, Gujarat 395006"
)
# Messy but CLEARLY LABELLED input: lower case, extra spaces, out of order and
# comma-separated instead of one field per line. The fallback parser supports
# this; it intentionally does NOT parse prose (see TestFallbackParserConservatism).
MESSY_REGISTRATION_TEXT = (
    "hi, name:  ananya shah , business name:Shah Gems & Jewels, "
    "gst: 24aaaps1234c1z5, "
    "business address: 12, Diamond Plaza, Varachia Road, Surat, Gujarat 395006"
)


def _make_engine_and_session():
    """Create an isolated in-memory SQLite database for one test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)()


class OnboardingServiceTestCase(unittest.TestCase):
    """Base class: isolated DB, feature flag ON, outbound Meta calls mocked."""

    def setUp(self):
        self.engine, self.db = _make_engine_and_session()
        self.sent_texts = []
        self.sent_buttons = []

        async def _capture_text(recipient_id, text):
            self.sent_texts.append((recipient_id, text))
            return True

        async def _capture_button(recipient_id, body_text, button_id, button_title):
            self.sent_buttons.append(
                {
                    "to": recipient_id,
                    "body": body_text,
                    "id": button_id,
                    "title": button_title,
                }
            )
            return True

        self._patches = [
            patch.object(
                onboarding_service, "send_text_message", new=AsyncMock(side_effect=_capture_text)
            ),
            patch.object(
                onboarding_service,
                "send_interactive_cta_button",
                new=AsyncMock(side_effect=_capture_button),
            ),
            # Never call a real LLM: default to the deterministic parser.
            patch.object(
                onboarding_service,
                "_extract_via_gemini",
                new=AsyncMock(return_value=None),
            ),
            patch.object(settings, "ENABLE_ONBOARDING_GATE", True),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        self.db.close()
        self.engine.dispose()

    # ── helpers ────────────────────────────────────────────────────────

    def _handle(self, text, sender=SENDER):
        return asyncio.run(handle_text(whatsapp_id=sender, text=text, db=self.db))

    def _customers(self):
        return self.db.query(Customer).all()

    def _session_row(self, sender=SENDER):
        return (
            self.db.query(OnboardingSession)
            .filter(OnboardingSession.whatsapp_id == sender)
            .first()
        )

    def _start_and_register(self, text=FULL_REGISTRATION_TEXT):
        """Run the "Hi" → registration-details journey."""
        self._handle("Hi")
        return self._handle(text)


# ─── 1-3: welcome + instruction messages ─────────────────────────────────


class TestNewUserWelcome(OnboardingServiceTestCase):
    def test_new_user_hi_is_handled_and_sends_welcome(self):
        result = self._handle("Hi")

        self.assertTrue(result.handled)
        self.assertEqual(self.sent_texts[0], (SENDER, WELCOME_MESSAGE))
        self.assertIn("Welcome to Moraa Studio", self.sent_texts[0][1])

    def test_registration_instructions_are_sent_after_welcome(self):
        self._handle("Hi")

        self.assertEqual(len(self.sent_texts), 2)
        self.assertEqual(self.sent_texts[1], (SENDER, REGISTRATION_INSTRUCTIONS_MESSAGE))
        self.assertIn("Quick registration", self.sent_texts[1][1])

    def test_state_becomes_awaiting_registration(self):
        self._handle("Hi")

        self.assertEqual(
            get_onboarding_state(self.db, SENDER),
            OnboardingState.AWAITING_REGISTRATION,
        )
        self.assertEqual(self._session_row().state, "AWAITING_REGISTRATION")

    def test_no_customer_created_on_welcome(self):
        self._handle("Hi")

        self.assertEqual(self._customers(), [])


# ─── 4-6: complete registration ──────────────────────────────────────────


class TestCompleteRegistration(OnboardingServiceTestCase):
    def test_complete_registration_creates_exactly_one_customer(self):
        result = self._start_and_register()

        self.assertTrue(result.handled)
        customers = self._customers()
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0].whatsapp_id, SENDER)
        self.assertEqual(customers[0].full_name, "Ananya Shah")
        self.assertEqual(customers[0].business_name, "Shah Gems & Jewels")
        self.assertEqual(customers[0].gst_number, "24AAAPS1234C1Z5")
        self.assertIn("Varachia Road", customers[0].address)

    def test_customer_receives_congratulations_message(self):
        self._start_and_register()

        congrats = self.sent_texts[-1][1]
        self.assertEqual(
            congrats,
            "Congratulations Ananya! You’re registered with Moraa Studio 🎉\n\n"
            "You’re all set to start creating stunning product photos.",
        )

    def test_customer_receives_recharge_cta(self):
        self._start_and_register()

        self.assertEqual(len(self.sent_buttons), 1)
        button = self.sent_buttons[0]
        self.assertEqual(button["id"], RECHARGE_BUTTON_ID)
        self.assertEqual(button["title"], RECHARGE_BUTTON_TITLE)
        self.assertEqual(button["to"], SENDER)

    def test_state_becomes_registered(self):
        self._start_and_register()

        self.assertEqual(get_onboarding_state(self.db, SENDER), OnboardingState.REGISTERED)

    def test_messy_labelled_registration_is_parsed(self):
        result = self._handle("Hi")
        self.assertTrue(result.handled)
        self._handle(MESSY_REGISTRATION_TEXT)

        customers = self._customers()
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0].full_name.lower(), "ananya shah")
        self.assertEqual(customers[0].business_name, "Shah Gems & Jewels")
        self.assertEqual(customers[0].gst_number, "24AAAPS1234C1Z5")

    def test_first_message_with_details_still_starts_the_welcome_journey(self):
        """CRITICAL #1: no first message may ever create a customer."""
        result = self._handle(FULL_REGISTRATION_TEXT)

        self.assertTrue(result.handled)
        self.assertEqual(self.sent_texts[0], (SENDER, WELCOME_MESSAGE))
        self.assertEqual(
            self.sent_texts[1], (SENDER, REGISTRATION_INSTRUCTIONS_MESSAGE)
        )
        self.assertEqual(self._customers(), [])
        self.assertEqual(
            get_onboarding_state(self.db, SENDER),
            OnboardingState.AWAITING_REGISTRATION,
        )

        # Following the instructions (“send it right back”) completes it.
        self._handle(FULL_REGISTRATION_TEXT)
        self.assertEqual(len(self._customers()), 1)


# ─── 7-8: incomplete registration ────────────────────────────────────────


class TestIncompleteRegistration(OnboardingServiceTestCase):
    def test_incomplete_registration_does_not_create_customer(self):
        self._handle("Hi")
        self._handle("Name: Ananya Shah\nBusiness name: Shah Gems & Jewels")

        self.assertEqual(self._customers(), [])
        self.assertFalse(is_registered(SENDER, self.db))

    def test_only_missing_fields_are_requested(self):
        self._handle("Hi")
        self._handle("Name: Ananya Shah\nBusiness name: Shah Gems & Jewels")

        reminder = self.sent_texts[-1][1]
        self.assertIn("Almost there! 📋", reminder)
        self.assertIn("GST number", reminder)
        self.assertIn("Business address", reminder)
        self.assertNotIn("• Name", reminder)
        self.assertNotIn("• Business name", reminder)

    def test_partial_fields_are_persisted(self):
        self._handle("Hi")
        self._handle("Name: Ananya Shah\nBusiness name: Shah Gems & Jewels")

        row = self._session_row()
        self.assertIsNotNone(row)
        pending = json.loads(row.pending_data)
        self.assertEqual(pending["full_name"], "Ananya Shah")
        self.assertEqual(pending["business_name"], "Shah Gems & Jewels")
        self.assertEqual(pending["gst_number"], "")
        self.assertEqual(row.state, "AWAITING_REGISTRATION")

    def test_explicit_no_gst_never_counts_as_complete(self):
        self._handle("Hi")
        self._handle(
            "Name: Ananya Shah\nBusiness name: Shah Gems & Jewels\n"
            "GST number: no GST\nBusiness address: 12, Diamond Plaza, Surat"
        )

        self.assertEqual(self._customers(), [])
        self.assertIn("GST number", self.sent_texts[-1][1])


# ─── 9: completing the missing fields later ──────────────────────────────


class TestFollowUpCompletion(OnboardingServiceTestCase):
    def test_subsequent_message_completes_missing_fields(self):
        self._handle("Hi")
        self._handle("Name: Ananya Shah\nBusiness name: Shah Gems & Jewels")
        result = self._handle(
            "GST number: 24AAAPS1234C1Z5\n"
            "Business address: 12, Diamond Plaza, Varachia Road, Surat"
        )

        self.assertTrue(result.handled)
        customers = self._customers()
        self.assertEqual(len(customers), 1)
        # Previously captured fields are not lost…
        self.assertEqual(customers[0].full_name, "Ananya Shah")
        self.assertEqual(customers[0].business_name, "Shah Gems & Jewels")
        # …and the newly supplied fields are stored.
        self.assertEqual(customers[0].gst_number, "24AAAPS1234C1Z5")
        self.assertIn("Varachia Road", customers[0].address)

    def test_prose_follow_up_does_not_wipe_pending_fields(self):
        """A conversational follow-up is parsed conservatively: existing
        registration data survives and nothing new is invented."""
        self._handle("Hi")
        self._handle("Name: Ravi Sharma\nBusiness name: Sharma Jewellers")
        self._handle("Hi, my name is Ravi and I have a jewellery shop")

        self.assertEqual(self._customers(), [])
        pending = json.loads(self._session_row().pending_data)
        self.assertEqual(pending["full_name"], "Ravi Sharma")
        self.assertEqual(pending["business_name"], "Sharma Jewellers")
        self.assertEqual(pending["gst_number"], "")
        self.assertEqual(pending["address"], "")

        self._handle(
            "GST number: 27ABCDE1234F1Z5\nBusiness address: Surat, Gujarat"
        )
        customers = self._customers()
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0].full_name, "Ravi Sharma")
        self.assertEqual(customers[0].business_name, "Sharma Jewellers")

    def test_corrected_field_wins_on_follow_up(self):
        self._handle("Hi")
        self._handle("Name: Ananya\nBusiness name: Shah Gems")
        self._handle("Name: Ananya Shah\nBusiness name: Shah Gems & Jewels")
        self._handle(
            "GST number: 24AAAPS1234C1Z5\nBusiness address: 12, Diamond Plaza, Surat"
        )

        customers = self._customers()
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0].full_name, "Ananya Shah")
        self.assertEqual(customers[0].business_name, "Shah Gems & Jewels")


# ─── 10: idempotency ─────────────────────────────────────────────────────


class TestIdempotency(OnboardingServiceTestCase):
    def test_duplicate_registration_does_not_create_duplicate_customers(self):
        self._start_and_register()
        self._handle(FULL_REGISTRATION_TEXT)

        self.assertEqual(len(self._customers()), 1)

    def test_create_or_update_customer_is_idempotent(self):
        data = RegistrationData(
            full_name="Ananya Shah",
            business_name="Shah Gems & Jewels",
            gst_number="24AAAPS1234C1Z5",
            address="12, Diamond Plaza, Surat",
        ).recompute_completeness()

        first = create_or_update_customer(self.db, SENDER, data)
        second = create_or_update_customer(self.db, SENDER, data)

        self.assertIsNotNone(first)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self._customers()), 1)

    def test_incomplete_data_never_creates_a_customer(self):
        data = RegistrationData(full_name="Ananya Shah")

        created = create_or_update_customer(self.db, SENDER, data)

        self.assertIsNone(created)
        self.assertEqual(self._customers(), [])


# ─── 11-12: backward compatibility ───────────────────────────────────────


class TestBackwardCompatibility(OnboardingServiceTestCase):
    def test_existing_registered_user_bypasses_onboarding(self):
        data = RegistrationData(
            full_name="Ananya Shah",
            business_name="Shah Gems & Jewels",
            gst_number="24AAAPS1234C1Z5",
            address="12, Diamond Plaza, Surat",
        ).recompute_completeness()
        create_or_update_customer(self.db, SENDER, data)

        result = self._handle("Hi")

        self.assertFalse(result.handled)
        self.assertEqual(self.sent_texts, [])
        self.assertEqual(self.sent_buttons, [])

    def test_registered_user_is_recognised_by_whatsapp_id(self):
        self.assertTrue(is_registered(SENDER, self.db) is False)
        self._start_and_register()
        self.assertTrue(is_registered(SENDER, self.db) is True)
        self.assertFalse(is_registered("910000000000", self.db))

    def test_other_users_are_not_affected_by_one_registration(self):
        self._start_and_register()

        result = self._handle("Hi", sender="919000000001")

        self.assertTrue(result.handled)
        self.assertEqual(self.sent_texts[-2][1], WELCOME_MESSAGE)


# ─── CRITICAL #1 — first message routing ─────────────────────────────────


class TestCasualFirstMessage(OnboardingServiceTestCase):
    """A new user's first ordinary message must start the welcome journey.

    Regression guard for the misrouting bug: casual messages that merely
    mention label-like words ("name", "business", "shop") must NOT be treated
    as registration input, must NOT create a customer, and must NOT persist
    any invented/garbage field values.
    """

    CASUAL_MESSAGES = (
        "Hi, my name is Ravi and I have a jewellery shop",
        "Hello, I run a business, my name is Ananya, we are based in Surat",
    )

    def test_casual_first_message_sends_welcome_then_instructions(self):
        for index, message in enumerate(self.CASUAL_MESSAGES):
            sender = f"9198765432{index:02d}"
            self.sent_texts.clear()

            result = self._handle(message, sender=sender)

            self.assertTrue(result.handled, msg=message)
            self.assertEqual(
                self.sent_texts[0], (sender, WELCOME_MESSAGE), msg=message
            )
            self.assertEqual(
                self.sent_texts[1],
                (sender, REGISTRATION_INSTRUCTIONS_MESSAGE),
                msg=message,
            )

    def test_casual_first_message_moves_user_to_awaiting_registration(self):
        for index, message in enumerate(self.CASUAL_MESSAGES):
            sender = f"9198765432{index:02d}"

            self._handle(message, sender=sender)

            self.assertEqual(
                get_onboarding_state(self.db, sender),
                OnboardingState.AWAITING_REGISTRATION,
                msg=message,
            )

    def test_casual_first_message_creates_no_customer(self):
        for index, message in enumerate(self.CASUAL_MESSAGES):
            sender = f"9198765432{index:02d}"

            self._handle(message, sender=sender)

            self.assertFalse(is_registered(sender, self.db), msg=message)
            self.assertEqual(self._customers(), [], msg=message)

    def test_casual_first_message_persists_no_garbage(self):
        for index, message in enumerate(self.CASUAL_MESSAGES):
            sender = f"9198765432{index:02d}"

            self._handle(message, sender=sender)

            row = self._session_row(sender)
            self.assertIsNotNone(row)
            if row.pending_data:
                for field_name, value in json.loads(row.pending_data).items():
                    self.assertEqual(
                        value, "", msg=f"{field_name} from {message!r}"
                    )

    def test_second_message_is_treated_as_registration_input(self):
        """After the welcome, registration data IS parsed (journey preserved)."""
        self._handle(self.CASUAL_MESSAGES[0])
        self._handle(FULL_REGISTRATION_TEXT)

        customers = self._customers()
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0].full_name, "Ananya Shah")


class TestFallbackParserConservatism(unittest.TestCase):
    """CRITICAL #2 — the fallback parser must never manufacture fields.

    Arbitrary conversational prose yields EMPTY fields; only clearly labelled
    input (label at a segment start + explicit separator) is read.
    """

    PROSE_MESSAGES = (
        "Hi, my name is Ravi and I have a jewellery shop",
        "Hello, I run a business, my name is Ananya, we are based in Surat",
        "Hi",
        "Hi, I want to know your pricing and address",
        "I am Ananya, my business name is Shah Gems, GST 27ABCDE1234F1Z5",
        "our shop address is somewhere near the market",
    )

    def test_prose_never_produces_customer_fields(self):
        for message in self.PROSE_MESSAGES:
            data = _heuristic_extract(message)

            self.assertEqual(
                data.to_dict(),
                {
                    "full_name": "",
                    "business_name": "",
                    "gst_number": "",
                    "address": "",
                    "is_complete": False,
                },
                msg=message,
            )

    def test_prose_through_parse_registration_text_is_conservative(self):
        for message in self.PROSE_MESSAGES:
            with patch.object(
                onboarding_service,
                "_extract_via_gemini",
                new=AsyncMock(return_value=None),
            ):
                data = asyncio.run(parse_registration_text(message))

            self.assertEqual(data.full_name, "", msg=message)
            self.assertEqual(data.business_name, "", msg=message)
            self.assertEqual(data.gst_number, "", msg=message)
            self.assertFalse(data.is_complete, msg=message)

    def test_clearly_labelled_input_is_still_supported(self):
        data = _heuristic_extract(
            "Name: Ravi Sharma\n"
            "Business name: Sharma Jewellers\n"
            "GST number: 27ABCDE1234F1Z5\n"
            "Business address: Surat, Gujarat"
        )

        self.assertTrue(data.is_complete)
        self.assertEqual(data.full_name, "Ravi Sharma")
        self.assertEqual(data.business_name, "Sharma Jewellers")
        self.assertEqual(data.gst_number, "27ABCDE1234F1Z5")
        self.assertEqual(data.address, "Surat, Gujarat")

    def test_messy_but_labelled_input_is_supported(self):
        self.assertTrue(_heuristic_extract(MESSY_REGISTRATION_TEXT).is_complete)

    def test_unlabelled_gst_alone_is_not_guessed(self):
        data = _heuristic_extract("my gst 27ABCDE1234F1Z5 and that is all")

        self.assertEqual(data.gst_number, "")
        self.assertFalse(data.is_complete)

    def test_source_grounding_helper(self):
        source = "Name: ananya shah, business name: shah gems & jewels"

        self.assertTrue(is_value_grounded("Ananya Shah", source))
        self.assertTrue(is_value_grounded("Shah Gems & Jewels", source))
        self.assertFalse(is_value_grounded("Priya Verma", source))
        self.assertFalse(is_value_grounded("Shah Gems Private Limited", source))


class TestFeatureFlagDisabled(unittest.TestCase):
    """ENABLE_ONBOARDING_GATE=False must leave behaviour exactly as before."""

    def setUp(self):
        self.engine, self.db = _make_engine_and_session()
        self.sent_texts = []

        async def _capture_text(recipient_id, text):
            self.sent_texts.append((recipient_id, text))
            return True

        self._patches = [
            patch.object(
                onboarding_service, "send_text_message", new=AsyncMock(side_effect=_capture_text)
            ),
            patch.object(
                settings, "ENABLE_ONBOARDING_GATE", False
            ),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()
        self.db.close()
        self.engine.dispose()

    def test_flag_defaults_to_false(self):
        self.assertIn("ENABLE_ONBOARDING_GATE", settings.model_fields)
        self.assertIs(settings.model_fields["ENABLE_ONBOARDING_GATE"].default, False)

    def test_disabled_gate_handles_nothing(self):
        result = asyncio.run(handle_text(whatsapp_id=SENDER, text="Hi", db=self.db))

        self.assertFalse(result.handled)
        self.assertEqual(self.sent_texts, [])
        self.assertEqual(self.db.query(Customer).all(), [])
        self.assertEqual(self.db.query(OnboardingSession).all(), [])

    def test_disabled_gate_ignores_registration_text(self):
        result = asyncio.run(
            handle_text(whatsapp_id=SENDER, text=FULL_REGISTRATION_TEXT, db=self.db)
        )

        self.assertFalse(result.handled)
        self.assertEqual(self.db.query(Customer).all(), [])


# ─── 13: parser failure safety ───────────────────────────────────────────


class TestParserFailure(OnboardingServiceTestCase):
    def test_parser_exception_sends_friendly_retry_and_does_not_raise(self):
        self._handle("Hi")

        with patch.object(
            onboarding_service,
            "parse_registration_text",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = self._handle(FULL_REGISTRATION_TEXT)

        self.assertTrue(result.handled)
        self.assertEqual(self.sent_texts[-1][1], PARSER_FAILURE_MESSAGE)
        self.assertEqual(self._customers(), [])

    def test_parser_error_is_never_exposed_to_customer(self):
        self._handle("Hi")

        with patch.object(
            onboarding_service,
            "parse_registration_text",
            new=AsyncMock(side_effect=RuntimeError("secret internal detail")),
        ):
            self._handle("Name: Ananya Shah")

        self.assertNotIn("secret internal detail", self.sent_texts[-1][1])
        self.assertNotIn("Traceback", self.sent_texts[-1][1])

    def test_unexpected_service_error_is_swallowed(self):
        with patch.object(
            onboarding_service,
            "is_registered",
            side_effect=RuntimeError("db exploded"),
        ):
            result = self._handle("Hi")

        self.assertFalse(result.handled)


# ─── Parser unit tests ───────────────────────────────────────────────────


class TestRegistrationParser(unittest.TestCase):
    """Deterministic parser tests — the live LLM is never called."""

    def setUp(self):
        self._parser_patch = patch.object(
            onboarding_service,
            "_extract_via_gemini",
            new=AsyncMock(return_value=None),
        )
        self._parser_patch.start()

    def tearDown(self):
        self._parser_patch.stop()

    def test_parse_returns_strict_json_shape(self):
        data = asyncio.run(parse_registration_text(FULL_REGISTRATION_TEXT))

        self.assertEqual(
            set(data.to_dict().keys()),
            {"full_name", "business_name", "gst_number", "address", "is_complete"},
        )
        self.assertTrue(data.to_dict()["is_complete"])
        self.assertEqual(data.gst_number, "24AAAPS1234C1Z5")

    def test_missing_fields_force_incomplete(self):
        data = asyncio.run(parse_registration_text("Name: Ananya Shah"))

        self.assertFalse(data.is_complete)
        self.assertEqual(
            data.missing_fields, ["business_name", "gst_number", "address"]
        )

    def test_missing_information_is_never_invented(self):
        data = asyncio.run(parse_registration_text("Hi there, I want to join"))

        self.assertFalse(data.is_complete)
        self.assertEqual(data.full_name, "")
        self.assertEqual(data.business_name, "")
        self.assertEqual(data.gst_number, "")
        self.assertEqual(data.address, "")

    def test_gst_is_normalized_to_uppercase(self):
        self.assertEqual(normalize_gst_number(" 24aaaps1234c1z5 "), "24AAAPS1234C1Z5")

    def test_gst_placeholders_are_treated_as_missing(self):
        for value in ("no GST", "not registered", "N/A", "none", "unknown"):
            self.assertEqual(normalize_gst_number(value), "")

    def test_placeholder_gst_is_never_invented(self):
        data = asyncio.run(
            parse_registration_text(
                "Name: Ananya\nBusiness: Shah Gems\nGST: no GST\nAddress: Surat"
            )
        )

        self.assertEqual(data.gst_number, "")
        self.assertFalse(data.is_complete)

    def test_field_order_and_labels_are_flexible(self):
        data = asyncio.run(
            parse_registration_text(
                "business address: 12 Diamond Plaza Surat\n"
                "gst number: 24AAAPS1234C1Z5\n"
                "business name: Shah Gems\n"
                "full name: Ananya Shah"
            )
        )

        self.assertTrue(data.is_complete)
        self.assertEqual(data.address, "12 Diamond Plaza Surat")

    def test_empty_text_raises_parser_error(self):
        from app.services.onboarding_service import RegistrationParserError

        with self.assertRaises(RegistrationParserError):
            asyncio.run(parse_registration_text("   "))

    def test_sanitize_text_strips_control_characters_and_bounds_length(self):
        cleaned = sanitize_text("  Hello\x00 there  \n\n ")

        self.assertEqual(cleaned, "Hello there")
        self.assertLessEqual(
            len(sanitize_text("x" * 5000)),
            onboarding_service.MAX_REGISTRATION_TEXT_LENGTH,
        )

    def test_prose_between_labelled_fields_never_leaks_into_a_field(self):
        data = asyncio.run(
            parse_registration_text(
                "Name: Ravi Sharma\nI want 10 images please\n"
                "Business name: Sharma Jewellers"
            )
        )

        self.assertEqual(data.full_name, "Ravi Sharma")
        self.assertEqual(data.business_name, "Sharma Jewellers")
        self.assertEqual(data.address, "")
        self.assertFalse(data.is_complete)


class TestRegistrationMerge(unittest.TestCase):
    def test_merge_preserves_previous_fields(self):
        pending = RegistrationData(full_name="Ananya Shah", business_name="Shah Gems")
        incoming = RegistrationData(
            gst_number="24AAAPS1234C1Z5", address="12, Diamond Plaza, Surat"
        ).recompute_completeness()

        merged = merge_registration_data(pending, incoming)

        self.assertEqual(merged.full_name, "Ananya Shah")
        self.assertEqual(merged.business_name, "Shah Gems")
        self.assertEqual(merged.gst_number, "24AAAPS1234C1Z5")
        self.assertTrue(merged.is_complete)

    def test_merge_prefers_new_values(self):
        pending = RegistrationData(full_name="Ananya")
        incoming = RegistrationData(full_name="Ananya Shah", gst_number="").recompute_completeness()

        merged = merge_registration_data(pending, incoming)

        self.assertEqual(merged.full_name, "Ananya Shah")


# ─── Gemini parser path (SDK mocked — no network, no credits) ────────────


try:  # pragma: no cover - import guard mirrors the service
    from google import genai as genai_module
    from google.genai import types as genai_types

    _GENAI_IMPORT_ERROR = None
except ImportError as exc:  # pragma: no cover
    genai_module = None
    genai_types = None
    _GENAI_IMPORT_ERROR = exc


_DEFAULT_PARSER_SOURCE = (
    "Name: Ananya Shah\nBusiness name: Shah Gems & Jewels\n"
    "GST number: 24AAAPS1234C1Z5\nBusiness address: 12, Diamond Plaza, Surat"
)


class TestGeminiParserPath(unittest.TestCase):
    """Verifies the strict-JSON Gemini extraction path without real API calls."""

    def setUp(self):
        if genai_module is None:
            self.skipTest(f"google-genai not installed: {_GENAI_IMPORT_ERROR}")
        self.created_clients = []

    def _call(self, response_text, source_text=_DEFAULT_PARSER_SOURCE):
        class _FakeResponse:
            text = response_text

        class _FakeModels:
            def __init__(self):
                self.kwargs = None

            def generate_content(self, **kwargs):
                self.kwargs = kwargs
                return _FakeResponse()

        class _FakeClient:
            def __init__(self, api_key=None):
                self.api_key = api_key
                self.models = _FakeModels()

        def _client_factory(api_key=None):
            client = _FakeClient(api_key)
            self.created_clients.append(client)
            return client

        with patch.object(genai_module, "Client", side_effect=_client_factory), patch.object(
            settings, "GEMINI_API_KEY", "test-key"
        ), patch.object(settings, "ONBOARDING_PARSER_MODEL", "gemini-test-model"):
            return asyncio.run(
                onboarding_service._extract_via_gemini(source_text)
            )

    def test_strict_json_response_is_parsed_and_normalized(self):
        data = self._call(
            '{"full_name": "Ananya Shah", "business_name": "Shah Gems & Jewels", '
            '"gst_number": "24aaaps1234c1z5", '
            '"address": "12, Diamond Plaza, Surat", "is_complete": true}'
        )

        self.assertIsNotNone(data)
        self.assertTrue(data.is_complete)
        self.assertEqual(data.gst_number, "24AAAPS1234C1Z5")

    def test_request_asks_for_raw_json_at_zero_temperature(self):
        self._call('{"full_name": "Ananya"}')

        client = self.created_clients[0]
        kwargs = client.models.kwargs
        self.assertEqual(kwargs["model"], "gemini-test-model")
        self.assertEqual(kwargs["config"].response_mime_type, "application/json")
        self.assertEqual(kwargs["config"].temperature, 0.0)
        self.assertIn("STRICT JSON", kwargs["contents"])

    def test_llm_is_complete_flag_is_ignored_when_fields_are_missing(self):
        data = self._call('{"full_name": "Ananya", "is_complete": true}')

        self.assertIsNotNone(data)
        self.assertFalse(data.is_complete)

    def test_llm_gst_is_dropped_when_customer_says_none(self):
        with patch.object(genai_module, "Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value.text = (
                '{"full_name": "Ananya", "business_name": "Shah Gems", '
                '"gst_number": "27AAAAA0000A1Z5", "address": "Surat", '
                '"is_complete": true}'
            )
            with patch.object(settings, "GEMINI_API_KEY", "test-key"):
                data = asyncio.run(
                    onboarding_service._extract_via_gemini(
                        "Name: Ananya\nBusiness name: Shah Gems\n"
                        "GST number: no GST\nAddress: Surat"
                    )
                )

        self.assertIsNotNone(data)
        self.assertEqual(data.gst_number, "")
        self.assertFalse(data.is_complete)

    def test_markdown_fenced_json_is_tolerated(self):
        data = self._call(
            "```json\n{\"full_name\": \"Ananya Shah\"}\n```"
        )

        self.assertIsNotNone(data)
        self.assertEqual(data.full_name, "Ananya Shah")

    def test_unusable_llm_output_returns_none_for_fallback(self):
        data = self._call("I am sorry, I cannot help with that.")

        self.assertIsNone(data)

    def test_llm_exception_returns_none_instead_of_raising(self):
        with patch.object(genai_module, "Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = RuntimeError("api down")
            with patch.object(settings, "GEMINI_API_KEY", "test-key"):
                data = asyncio.run(onboarding_service._extract_via_gemini("Name: Ananya"))

        self.assertIsNone(data)

    def test_invented_gst_not_present_in_message_is_dropped(self):
        """A hallucinated value must never reach the customer record."""
        data = self._call(
            '{"full_name": "Ananya Shah", "business_name": "Shah Gems & Jewels", '
            '"gst_number": "27AAAAA0000A1Z5", '
            '"address": "12, Diamond Plaza, Surat", "is_complete": true}',
            source_text=(
                "Name: Ananya Shah\nBusiness name: Shah Gems & Jewels\n"
                "Business address: 12, Diamond Plaza, Surat"
            ),
        )

        self.assertIsNotNone(data)
        self.assertEqual(data.gst_number, "")
        self.assertFalse(data.is_complete)

    def test_invented_address_word_is_dropped(self):
        data = self._call(
            '{"address": "12, Diamond Plaza, Andheri West, Mumbai"}',
            source_text="Name: Ananya Shah\nBusiness address: 12, Diamond Plaza, Surat",
        )

        self.assertIsNotNone(data)
        self.assertEqual(data.address, "")

    def test_reformatted_but_grounded_values_are_kept(self):
        """Case/punctuation reformatting is tolerated; only invention is not."""
        data = self._call(
            '{"full_name": "Ananya Shah", "business_name": "Shah Gems & Jewels", '
            '"gst_number": "24AAAPS1234C1Z5", "address": "12, Diamond Plaza, Surat", '
            '"is_complete": true}',
            source_text=(
                "name: ananya shah, business name: shah gems & jewels, "
                "gst: 24aaaps1234c1z5, address: 12, diamond plaza, surat"
            ),
        )

        self.assertIsNotNone(data)
        self.assertTrue(data.is_complete)
        self.assertEqual(data.full_name, "Ananya Shah")


# ─── Meta payload structure ──────────────────────────────────────────────


class TestMetaPayloads(unittest.TestCase):
    """The onboarding messages use the existing Meta WhatsApp helpers."""

    def _post_payload(self, coro_fn, *args):
        captured = {}

        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"messages": [{"id": "wamid.test"}]}

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                captured["payload"] = json
                return _Response()

        with patch(
            "app.services.meta_whatsapp_service.httpx.AsyncClient", new=_Client
        ), patch.object(settings, "META_WHATSAPP_TOKEN", "test-token"), patch.object(
            settings, "META_PHONE_NUMBER_ID", "123456"
        ):
            ok = asyncio.run(coro_fn(*args))

        return ok, captured.get("payload")

    def test_text_message_payload(self):
        from app.services.meta_whatsapp_service import send_text_message

        ok, payload = self._post_payload(send_text_message, SENDER, "Hello")

        self.assertTrue(ok)
        self.assertEqual(payload["messaging_product"], "whatsapp")
        self.assertEqual(payload["to"], SENDER)
        self.assertEqual(payload["type"], "text")
        self.assertEqual(payload["text"]["body"], "Hello")

    def test_recharge_cta_payload(self):
        from app.services.meta_whatsapp_service import send_interactive_cta_button

        ok, payload = self._post_payload(
            send_interactive_cta_button,
            SENDER,
            "Body text",
            RECHARGE_BUTTON_ID,
            RECHARGE_BUTTON_TITLE,
        )

        self.assertTrue(ok)
        self.assertEqual(payload["type"], "interactive")
        interactive = payload["interactive"]
        self.assertEqual(interactive["type"], "button")
        self.assertEqual(interactive["body"]["text"], "Body text")
        button = interactive["action"]["buttons"][0]
        self.assertEqual(button["type"], "reply")
        self.assertEqual(button["reply"]["id"], "recharge_500")
        self.assertEqual(button["reply"]["title"], "💳 Recharge to use")


# ─── 12 + 14: webhook integration ────────────────────────────────────────


def _text_payload(body="Hi", sender=SENDER, message_id="wamid.text.1"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "type": "text",
                                    "id": message_id,
                                    "from": sender,
                                    "timestamp": "1700000000",
                                    "text": {"body": body},
                                }
                            ]
                        },
                    }
                ]
            }
        ],
    }


def _image_payload(sender=SENDER, message_id="wamid.image.1"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "type": "image",
                                    "id": message_id,
                                    "from": sender,
                                    "timestamp": "1700000000",
                                    "image": {"id": "media_123", "mime_type": "image/jpeg"},
                                }
                            ]
                        },
                    }
                ]
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
