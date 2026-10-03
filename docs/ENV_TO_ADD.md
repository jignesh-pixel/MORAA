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
