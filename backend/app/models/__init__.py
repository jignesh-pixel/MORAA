"""Database models package.

All models must be imported here so that ``Base.metadata.create_all()``
in ``database.py`` discovers them for table creation.
"""

from app.models.user import User
from app.models.image import Image
from app.models.analysis import Analysis
from app.models.report import Report
from app.models.history import HistoryEntry
from app.models.audit_log import AuditLog
from app.models.processing_log import ProcessingLog
from app.models.tool_execution import ToolExecutionLog
from app.models.retry_history import RetryHistory
from app.models.version_history import VersionHistory
from app.models.whatsapp_ingestion import WhatsAppIngestion
from app.models.customer import Customer
from app.models.onboarding_session import OnboardingSession
from app.models.whatsapp_payment_order import WhatsAppPaymentOrder
from app.models.wallet_transaction import WalletTransaction
from app.models.processed_message import ProcessedMessage
from app.models.pending_payment import PendingPayment
from app.models.razorpay_payment_link import RazorpayPaymentLink
from app.models.generation_spend import GenerationSpend
from app.models.revoked_token import RevokedToken

__all__ = [
    "WalletTransaction",
    "User",
    "Image",
    "Analysis",
    "Report",
    "HistoryEntry",
    "AuditLog",
    "ProcessingLog",
    "ToolExecutionLog",
    "RetryHistory",
    "VersionHistory",
    "WhatsAppIngestion",
    "Customer",
    "OnboardingSession",
    "WhatsAppPaymentOrder",
]
