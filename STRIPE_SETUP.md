# Stripe setup for CheriPic memberships

One-time wiring to make the new `/pricing` page + `/billing/*` endpoints
work end-to-end. ~15 min of dashboard clicking + env editing.

---

## 1 — Create three Products in Stripe

In <https://dashboard.stripe.com/products> (use the **Test** mode toggle while
developing):

| Product name             | Type    | Monthly price | Currency |
| ------------------------ | ------- | ------------- | -------- |
| `CheriPic Basic`         | Service | $0.00         | USD      |
| `CheriPic Premium Lite`  | Service | $9.99         | USD      |
| `CheriPic Premium`       | Service | $19.99        | USD      |

For each: **Add price → Recurring → Monthly → USD**. After saving you'll get
a Price ID that looks like `price_1Q…`. Copy each one.

> The Basic tier is free — no Price needed in Stripe. Only Premium Lite +
> Premium need real Price IDs.

---

## 2 — Paste the Price IDs into the two constant files

The frontend (for the Upgrade buttons) and the backend webhook (for the tier
flip after a successful checkout) both need to know which Price = which tier.

**Frontend** → `src/utils/constants/membership.ts`:

```ts
'premium-lite': {
  ...
  stripePriceId: 'price_REPLACE_PREMIUM_LITE',   // ← paste here
},
premium: {
  ...
  stripePriceId: 'price_REPLACE_PREMIUM',         // ← paste here
},
```

**Backend** → `ai_integration/fastapi/tier_limits.py`:

```python
STRIPE_PRICE_TO_TIER: dict[str, str] = {
    "price_REPLACE_PREMIUM_LITE": "premium-lite",   # ← paste here
    "price_REPLACE_PREMIUM":      "premium",        # ← paste here
}
```

Same IDs in both files. If they drift, the webhook can't map a paid sub
to a tier and the user is stuck on Basic after paying.

---

## 3 — Get your secret key + create a webhook

In <https://dashboard.stripe.com/apikeys>:

- Copy the **Secret key** (`sk_test_…` while developing, `sk_live_…` in prod).

In <https://dashboard.stripe.com/webhooks> → **Add endpoint**:

- **Endpoint URL** (local dev): use a tunnel — `stripe listen --forward-to localhost:8000/billing/webhook` is easiest and prints the signing secret right in the terminal. For prod: `https://api.cheripic.com/billing/webhook`.
- **Events to listen for**:
  - `checkout.session.completed`
  - `customer.subscription.created`
  - `customer.subscription.updated`
  - `customer.subscription.deleted`
  - `invoice.payment_succeeded`
  - `invoice.payment_failed`

After saving, click the endpoint → **Reveal signing secret** (`whsec_…`).

---

## 4 — Put both secrets in the FastAPI `.env`

In `ai_integration/fastapi/.env`:

```dotenv
STRIPE_SECRET_KEY=sk_test_xxxxxxxx
STRIPE_WEBHOOK_SECRET=whsec_xxxxxxxx
```

(.env is gitignored — never commit these.)

---

## 5 — Run the SQL migration

In the Supabase SQL editor (**Run without RLS**), execute:

```
membership_billing.sql
```

Adds the Stripe columns to `user_memberships`, the `cheri_ai_daily_usage`
table, and the `stripe_events` idempotency table.

---

## 6 — Reinstall + restart the backend

```bash
cd ai_integration/fastapi
source ../venv/bin/activate          # or wherever your venv lives
pip install -r requirements.txt      # picks up the new `stripe` package
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

---

## 7 — End-to-end test (test mode)

1. Open the app, sign in.
2. Navigate to `/pricing`.
3. Click "Upgrade to Premium Lite".
4. Stripe Checkout opens — pay with test card `4242 4242 4242 4242`, any
   future expiry, any 3 digits.
5. You bounce back to `/pricing?checkout=success`. Within a couple of
   seconds (the webhook delivery) your "You're on Basic" pill should
   change to "Premium Lite".
6. Open the Cheri chat. The usage chip at the top should now read
   "30 Cheri messages left today" instead of "5".

If step 5 doesn't update, check:
- The `stripe_events` table — is the event there with `processed_at` set?
- FastAPI logs — search for "Stripe webhook" lines.
- `tier_limits.py` STRIPE_PRICE_TO_TIER — does the price ID actually match?

---

## Going live

When you flip to live mode:
1. Re-create the same Products + Prices under **live mode** in the Stripe
   dashboard (test and live are separate).
2. Update both Price IDs in `membership.ts` and `tier_limits.py`.
3. Swap `sk_test_…` for `sk_live_…` in `.env`.
4. Re-create the webhook in live mode (new signing secret) and swap that too.
5. Tell users they can self-cancel from Stripe's [billing portal](https://stripe.com/docs/billing/subscriptions/customer-portal)
   — wiring that up is a single endpoint we haven't added yet; let me know
   when you want it.
