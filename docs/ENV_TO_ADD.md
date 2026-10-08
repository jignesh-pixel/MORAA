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

# Google Drive archive (optional); uses the business service account of the Phase 8 section below
DRIVE_ENABLED=true
DRIVE_FOLDER_ID=<id of the Drive folder to archive into>
```
Create the first login with `python scripts/create_dashboard_user.py --email you@example.com` (it asks for the password on the keyboard).

## Phase 8: SKU packs and Google Drive delivery (all off by default)
Switch on step by step (see `docs/PHASE_8_DEVELOPER_PLAN.html`, section 9). With both flags false nothing changes for customers.

```
# Packs (1/5/20/50/100 SKUs, price per SKU incl. GST). The price below is only the first seed:
# after `python scripts/set_price.py --sku-price N` the database price is used.
SKU_PACKS_ENABLED=false
SKU_PRICE_RUPEES=20
CATALOG_PACK_SKU_PRICE=500         # Catalog Pack (sku_pack_N) price per SKU; tiers are N x this
SKU_PACK_SIZES=1,5,20,50,100
ECOM_PACK1_ENABLED=true             # show the 1-SKU tier (Studio Shot products and the list menu)
SKU_GST_PERCENT=18
SKU_CREDIT_VALIDITY_DAYS=90
SKU_ERPNEXT_ITEM_CODE=              # ERPNext item for packs (empty = ERPNEXT_RECHARGE_ITEM_CODE)
META_CATALOG_ID=1853082239018330    # WhatsApp catalogue updated by set_price.py
# Catalogue retailer ids the bot sends and reads (tiers 1, 5, 20, 50, 100 from SKU_PACK_SIZES):
#   Studio Shot  (product set 1632701381728170): studio_sku_1 ... studio_sku_100
#   Catalog Pack (product set 1723543628708334): sku_pack_1 ... sku_pack_100

# Wallet recharge limits (unchanged values, now settings)
MIN_RECHARGE_RUPEES=500
MAX_RECHARGE_RUPEES=50000

# Google Drive delivery through the BUSINESS Workspace service account (no personal account anywhere)
DRIVE_DELIVERY_ENABLED=false
GOOGLE_SA_KEY_FILE=/run/secrets/moraa-drive-sa.json   # the downloaded JSON key; outside the code folder; never commit it
DRIVE_SHARED_DRIVE_ID=0AAZFDiQDkZfNUk9PVA
DRIVE_IMAGES_RETENTION_DAYS=90       # only runs with RETENTION_ENABLED=true (set it, or Drive images are kept forever)
BATCH_NOTIFY_DELAY_SECONDS=60
BATCH_NOTIFY_TEMPLATE_NAME=sku_batch_ready   # Meta utility template (folder link, SKUs left); empty = alert only
BATCH_NOTIFY_TEMPLATE_LANG=en

# The dashboard archive now uses the same service account; DRIVE_FOLDER_ID is a folder inside the shared drive.
DRIVE_ENABLED=false
DRIVE_FOLDER_ID=
```

Remove these from `.env` (personal Google account, retired): `GOOGLE_DRIVE_CLIENT_ID`, `GOOGLE_DRIVE_CLIENT_SECRET`, `GOOGLE_DRIVE_REFRESH_TOKEN`. An old `.env` that still has them starts fine; they are ignored.

Docker: mount the key file read-only into the `api` service (for example `- ./secrets/moraa-drive-sa.json:/run/secrets/moraa-drive-sa.json:ro`) and set `GOOGLE_SA_KEY_FILE` to that path.
