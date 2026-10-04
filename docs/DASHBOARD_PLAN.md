# WhatsApp chat dashboard: requirements captured 2026-10-03

Source: owner's answers plus `gemvision-order-flow.html`. Status: BUILT 2026-10-03 on branch phase-7-dashboard (backend API + Next.js page "WhatsApp Chats"); see OPERATIONS_RUNBOOK section 15. Not yet verified by the gate or reviewed.

- Users: the owner, the business partner and the developer. No staff roles. View only.
- Login: Google sign-in if practical, otherwise email and password.
- Purpose: weekly audits and customer complaints; everything about a customer visible in one place.
- Look: like a WhatsApp chat. Customer list; open a customer to see the chat (what they sent, what they received, images, invoices, payments in the stream). Clicking the phone number or saved name opens the profile: payments (what, when), name, business name, GSTIN as entered, balance, orders, invoices.
- History: 90 days (matches photo retention). Build for 90; configurable.
- Files: images are archived to the owner's personal Google Drive (5 TB plan, to be moved to a company drive later). The dashboard shows a preview; click to zoom; a "download" or "open" link goes to Drive. ERPNext invoices show a preview; "open" goes to ERPNext.
- Invoices: ERPNext issues all invoice numbers; the local PDF fallback is removed (done). Needs ERPNext credentials in the server settings.
- Consent notice must also say chats are kept for audit (to add to the notice text).
- Frontend: Next.js (the developer's choice). Hosting of the backend: AWS Lightsail.
- Order of build (agreed in principle): 1) save every message and photo as it happens (with status), 2) read-only API and dashboard, 3) Drive archive jobs, 4) ERPNext links and preview.
