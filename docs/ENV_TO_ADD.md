# Settings to add to backend/.env (the agent never edits that file)

Written 2026-10-03 from the owner's answers. Copy these lines into `backend/.env` on the server, then restart.

```
# Alerts go to both numbers (international format, no plus sign)
OPS_ALERT_WHATSAPP_NUMBERS=919699899825,919820666332

# Customer photos are deleted 90 days after a finished order; phone numbers in old audit notes are hidden after 30 days.
RETENTION_ENABLED=true
RETENTION_MEDIA_DAYS=90
RETENTION_AUDIT_MASK_DAYS=30

# Privacy notice shown before any data is collected (see the draft below; max 1024 characters)
CONSENT_REQUIRED=true
CONSENT_VERSION=1
CONSENT_NOTICE_TEXT=Welcome to Moraa Studio! To make your product images we keep your name, business name, GST number, address and the photos you send us. We use them only to create and deliver your images and to issue your invoices. By tapping I agree you allow us to do this.

# Error reports (after you create a Sentry project and run: pip install -r requirements.txt)
SENTRY_DSN=<paste the DSN from Sentry here>

# GST verification once you have chosen a paid vendor
GST_VERIFICATION_ENABLED=true
GST_PROVIDER=http
GST_API_URL=https://<vendor address>/<path>/{gstin}
GST_API_KEY=<your vendor key>
GST_API_KEY_HEADER=x-api-key
```

Notes:
- Alerts are sent from your business WhatsApp number to your two personal numbers. WhatsApp only lets a business message a person
  freely within 24 hours of that person last writing to the business, so an alert can fail if neither of you has messaged the
  business number for a day. Message the business number now and then, or ask us to add an approved message template.
- The consent notice above mentions what you asked for. It does not mention that photos go to AI providers outside India or how to
  delete data. Please let your adviser read it before switching it on.
- Razorpay offers a free GST lookup web page but no public API for it, so a separate vendor is needed (for example Cashfree
  Verification, Appyflow, Surepass or gstinapi.in; typical price is a few paise to two rupees per lookup). Tell us which one you open
  an account with and we will test it with their key.

## ERPNext invoices (decision 2026-10-03: ERPNext is the only invoice source)
```
ERPNEXT_INVOICE_ENABLED=true
ERPNEXT_BASE_URL=<your ERPNext address>
ERPNEXT_API_KEY=<key>
ERPNEXT_API_SECRET=<secret>
ERPNEXT_COMPANY=<company name in ERPNext>
ERPNEXT_RECHARGE_ITEM_CODE=<item code used for wallet recharges>
ERPNEXT_TAX_TEMPLATE=<GST template name>
ERPNEXT_DEFAULT_DEBTORS_ACCOUNT=<account>
ERPNEXT_PAYMENT_ACCOUNT=<account>
ERPNEXT_MODE_OF_PAYMENT=Razorpay
```
With this on there is no local PDF fallback: if ERPNext is down, the customer still gets the "payment received" message and the invoice
is retried automatically (6 tries over about two hours, then you get an alert).

## WhatsApp chat dashboard
```
# who may open the dashboard (besides ADMIN_USERNAMES); Google sign-in needs a Google OAuth client id
DASHBOARD_ALLOWED_EMAILS=you@gmail.com,partner@gmail.com
GOOGLE_CLIENT_ID=<optional: Google OAuth client id for "Sign in with Google">
# frontend (frontend/.env.local): NEXT_PUBLIC_API_URL=http://localhost:8000   NEXT_PUBLIC_GOOGLE_CLIENT_ID=<same client id>

# Google Drive archive (optional, off until all are set); uses the Drive owner's account
DRIVE_ENABLED=true
GOOGLE_DRIVE_CLIENT_ID=<oauth client id>
GOOGLE_DRIVE_CLIENT_SECRET=<oauth client secret>
GOOGLE_DRIVE_REFRESH_TOKEN=<refresh token of the Drive owner's account>
DRIVE_FOLDER_ID=<id of the Drive folder to archive into>
```
Create the first login with `python scripts/create_dashboard_user.py --email you@example.com` (it asks for the password on the keyboard).
