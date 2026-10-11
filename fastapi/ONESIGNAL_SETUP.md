# OneSignal Push Notifications — Setup & Wiring Guide

Added 2026-10-11. This doc covers:

1. [OneSignal account setup](#1-onesignal-account-setup)
2. [Env + DB migration](#2-env--db-migration)
3. [Frontend requirements](#3-frontend-requirements)
4. [Wiring push into the remaining 9 scenarios](#4-wiring-push-into-the-remaining-9-scenarios)
5. [Verification](#5-verification)

---

## 1. OneSignal account setup

1. Sign up / log in at <https://app.onesignal.com>.
2. Create a new **Web Push** app. Name it `CheriPic`.
3. Platform settings:
   - Site URL: `https://cheripic.com/application/` (match the deployed app root).
   - Default icon: upload the CheriPic logo (512×512 PNG).
   - Permission prompt: leave disabled for now (the frontend code calls `requestPushPermission()` at the right moment).
4. Settings → Keys & IDs → copy:
   - `App ID` → set as **ONESIGNAL_APP_ID** (backend) AND **VITE_ONESIGNAL_APP_ID** (frontend)
   - `REST API Key` → set as **ONESIGNAL_REST_API_KEY** (backend ONLY — never expose to the frontend)

---

## 2. Env + DB migration

### Backend env (Railway → Variables tab)
```
ONESIGNAL_APP_ID=<your-app-id>
ONESIGNAL_REST_API_KEY=<your-rest-api-key>
```

### Frontend env (.env in cheripic_pwa root)
```
VITE_ONESIGNAL_APP_ID=<your-app-id>
```

### Database migration
Apply once in Supabase SQL Editor:
```
cheriai_integration/fastapi/migrations/037_sent_push_audit.sql
```
Creates `public.sent_push` for audit + dedup. Idempotent.

---

## 3. Frontend requirements

Already wired (do not duplicate):

- `src/services/pushNotificationsService.ts` — `initPushNotifications()`, `identifyUser(uid)`, `clearUserIdentity()`, `requestPushPermission()`
- `src/App.tsx` boot → `initPushNotifications()` + re-alias if a session exists
- `src/pages/auth/login/Login.tsx` success handler → `identifyUser(uid)`
- `src/pages/profile/Profile.tsx` logout handler → `clearUserIdentity()`
- `public/OneSignalSDKWorker.js` — one-line SW that imports OneSignal's CDN worker

When you want to prompt a user for permission at a *moment of value* (e.g. right after they send their first interest), call:
```ts
import { requestPushPermission } from '@/services/pushNotificationsService';
void requestPushPermission(); // fire-and-forget
```

---

## 4. Wiring push into the remaining 9 scenarios

Scenario 1 (`interest_received`) is already wired as the reference. The pattern for each remaining scenario is the same: add a `send_push_notification(...)` call right after the existing `send_transactional_email(...)` call.

### Pattern (copy / adapt per site)
```python
from push_notifications import send_push_notification

await send_transactional_email(
    user_id=TARGET_USER_ID,
    template_slug="interest_accepted",
    extra_vars={"other_name": other_label},
    dedup_window_hours=2,
)

# Push alongside — same campaign slug for cross-channel audit, same
# recipient. url is where the OneSignal click handler sends them.
await send_push_notification(
    user_id=TARGET_USER_ID,
    title=f"{other_label} accepted your interest",
    body="Open your Red Room to say hi.",
    campaign="interest_accepted",
    url="/#/matching?tab=sent",
    dedup_window_hours=1,
)
```

### Per-scenario checklist

Each row is a one-line add in the file that already calls `send_transactional_email`. Keep campaign slugs identical to the email ones so `sent_emails` and `sent_push` share a dimension for ops dashboards.

| # | Scenario | File | Email call site | Push call to add |
|---|---|---|---|---|
| 2 | `interest_accepted` | `main.py` `/matching/notify-interest-responded` (accept branch) | `send_transactional_email(... "interest_accepted" ...)` | `send_push_notification(user_id=sender, title=f"{caller_label} accepted your interest", body="Open your Red Room to say hi.", campaign="interest_accepted", url="/#/matching?tab=sent", dedup_window_hours=1)` |
| 3 | `interest_declined` | `main.py` same handler, decline branch | `send_transactional_email(... "interest_declined" ...)` | `send_push_notification(user_id=sender, title="An update on your interest", body="They weren't ready — keep going.", campaign="interest_declined", url="/#/matching", dedup_window_hours=1)` |
| 4+5 | `connection_lost` | `partnership.py` finalize step 3d + step 3.5 | `send_transactional_email(... "connection_lost" ...)` | `send_push_notification(user_id=third_party, title="A connection has moved on", body="They partnered elsewhere. Discover is open.", campaign="connection_lost", url="/#/matching", dedup_window_hours=12)` |
| 6 | `red_room_message_unread` | `main.py` `/chat/notify-unread-message` | `send_transactional_email(... "red_room_message_unread" ...)` | `send_push_notification(user_id=receiver, title=f"{sender_label} sent you a message", body="Tap to open your Red Room.", campaign="red_room_message_unread", url=f"/#/chat/{caller_user_id}", dedup_window_hours=1)` |
| 7 | `partner_proposal_received` | `main.py` `/matching/notify-partner-proposal` | `send_transactional_email(... "partner_proposal_received" ...)` | `send_push_notification(user_id=other_id, title=f"{caller_label} wants to make it official 💞", body="Open your Red Room to review.", campaign="partner_proposal_received", url=f"/#/chat/{caller_user_id}", dedup_window_hours=1)` |
| 8 | `partner_proposal_accepted` | `partnership.py` finalize step 2b (celebration block) | `send_transactional_email(... "partner_proposal_accepted" ...)` | `send_push_notification(user_id=me, title=f"You and {nick_by_id.get(other)} are official 💞", body="New introductions are paused. Tap to open your chat.", campaign="partner_proposal_accepted", url=f"/#/chat/{other}", dedup_window_hours=24)` |
| 9 | `partner_proposal_declined` | `main.py` `/matching/notify-partner-proposal-declined` | `send_transactional_email(... "partner_proposal_declined" ...)` | `send_push_notification(user_id=proposer, title="An update on your request", body=f"{caller_label} wasn't ready this time.", campaign="partner_proposal_declined", url="/#/matching", dedup_window_hours=1)` |
| 10 | `assessment_completed` | `main.py` `/assessment/notify-completed` | `send_transactional_email(... "assessment_completed" ...)` | `send_push_notification(user_id=caller_user_id, title="Your readiness assessment is ready", body=f"You scored {req.score}/100.", campaign="assessment_completed", url="/#/matching", dedup_window_hours=24)` |

Total: 7 `await send_push_notification(...)` blocks to add. All share the same shape; copy / adapt title + body + url + dedup.

### Dedup window guidance

- **Transient events** (interest_received, interest_accepted, interest_declined, partner_proposal_*, red_room_message_unread, partner_proposal_declined): `1h` — enough to batch bursts without feeling slow.
- **Milestone events** (partner_proposal_accepted, assessment_completed): `24h` — repeat triggers rare, but a day lets re-takes / self-heal sweeps run safely.
- **Multi-recipient wide fan-out** (connection_lost): `12h` — compromise between burst protection and per-pair relevance.

---

## 5. Verification

### Backend reachable + envs loaded
```bash
curl -s https://cheriaiintegration-production.up.railway.app/health
# should return {"status": "ok", ...}
```

### Audit table live
```sql
SELECT COUNT(*) FROM public.sent_push;
-- should return 0 immediately after migration 037
```

### End-to-end push smoke test

1. Deploy frontend + backend (both have the new code).
2. Open the deployed app on a mobile device OR desktop Chrome.
3. Sign in as user A.
4. Grant notification permission when prompted (or trigger manually via DevTools: `(await OneSignal.Notifications.requestPermission(), OneSignal.Notifications.permission)`).
5. In OneSignal Dashboard → Audience → Subscriptions, confirm your user's `external_id` matches user A's Supabase uuid.
6. Sign in as user B on another device / browser.
7. User B sends interest to user A.
8. User A should receive both:
   - The `interest_received` email (if their 2h dedup window allows)
   - The push notification (if their 1h dedup window allows)
9. Verify audit rows:
```sql
SELECT campaign, status, sent_at
  FROM public.sent_push
 WHERE user_id = '<user_A_uuid>'
 ORDER BY sent_at DESC
 LIMIT 5;
```

### What "no push arrived" means

- `sent_push` row with `status=sent` + no notification visible → check OneSignal dashboard; usually user hasn't granted permission on this device yet.
- `sent_push` row with `status=failed` → inspect Railway logs for `[push] no delivery for ...`; most common cause is user A never completed the OneSignal subscription on any device.
- No `sent_push` row at all → env vars missing on backend; `[push] ONESIGNAL env not configured — skipping ...` will be in logs.

---

## Cost note

OneSignal's free tier covers unlimited web push subscriptions and messages. No per-send charges within the free plan. The `sent_push` audit table adds negligible Supabase storage (one row per sent push, text columns only).

The push send itself adds ~1 HTTP call to OneSignal per matched event (same frequency as email). No background polling, no cron. If OneSignal env is unset, the module early-returns before the HTTP call — zero cost.
