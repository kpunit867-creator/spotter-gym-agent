# Deploying Spotter for free

| Piece | Where | Cost |
|---|---|---|
| Database | Supabase (Mumbai) | Free |
| Backend (FastAPI) | Render free web service (Singapore) | Free, 750 h/month = always on for one service |
| Reminder timer + keep-alive | Supabase `pg_cron` calls `/cron/run-reminders` every 5 min | Free |
| LLM | Gemini 3.5 Flash-Lite free tier (500 requests/day) | Free |
| WhatsApp | Meta test number (up to 5 recipient numbers) | Free |

Render's free instance sleeps after 15 minutes without traffic and takes ~30–60 s to wake.
The 5-minute cron call keeps it awake, and doubles as the reminder scheduler.

## 1. WhatsApp app details (Meta developer dashboard)
Open your app → **WhatsApp → API Setup**.
1. **Phone number ID** (under the test number) → `WHATSAPP_PHONE_NUMBER_ID`
2. **Access token** → `WHATSAPP_ACCESS_TOKEN`. The one shown there expires in 24 h; for a lasting token create a
   System User in Business Settings → Users → System users, give it the app with `whatsapp_business_messaging`
   and `whatsapp_business_management`, and generate a token.
3. **To** field → add your own WhatsApp number as a test recipient and confirm the code WhatsApp sends you.
4. **App settings → Basic → App secret** → `WHATSAPP_APP_SECRET` (used to check webhook signatures).
5. `WHATSAPP_VERIFY_TOKEN` is already generated in `.env`; you'll paste it into Meta in step 3.

## 2. Try it locally first (optional, fastest feedback)
Meta needs a public HTTPS URL. A free Cloudflare quick tunnel gives your laptop one:
```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8000
```
Use the `https://….trycloudflare.com` URL it prints in step 3 (it changes every time you restart the tunnel).

## 3. Connect the webhook
Meta app → **WhatsApp → Configuration → Webhook → Edit**:
- Callback URL: `https://<your-host>/webhook/whatsapp`
- Verify token: the `WHATSAPP_VERIFY_TOKEN` value from `.env`
- Click **Verify and save**, then **Manage** → subscribe to **messages**.

Send "hi" to the test number from your WhatsApp. Onboarding should start.

## 4. Deploy on Render
1. Push the repo to GitHub (`.env` is git-ignored).
2. Render → **New → Blueprint** → pick the repo. It reads `render.yaml`.
3. Fill in the secret values when asked (same as your `.env`).
4. Once live, `https://spotter-backend-xxxx.onrender.com/health` should show 18/18 tables.
5. Point the Meta webhook (step 3) at the Render URL.

## 5. Reminders + keep-alive with Supabase pg_cron
Supabase → **Database → Extensions** → enable `pg_cron` and `pg_net`. Then in the SQL editor
(replace the URL and secret):
```sql
select cron.schedule(
  'spotter-reminders',
  '*/5 * * * *',
  $$ select net.http_post(
       url     := 'https://spotter-backend-xxxx.onrender.com/cron/run-reminders',
       headers := jsonb_build_object('Content-Type', 'application/json', 'X-Cron-Secret', '<CRON_SECRET>'),
       body    := '{}'::jsonb) $$
);
-- check it's running:  select * from cron.job_run_details order by start_time desc limit 5;
-- stop it:             select cron.unschedule('spotter-reminders');
```

## 6. Message templates (for reminders outside the 24-hour window)
Meta → **WhatsApp Manager → Message templates → Create**. Create `meal_log_reminder_v1` and
`workout_log_reminder_v1` (category Utility, language English) with the body text stored in the
`whatsapp_templates` table. Until they're approved, reminders only work inside the 24-hour window.

## Limits to know
- Test number: messages only reach the (max 5) numbers you verified in step 1.3.
- Free Render: cold start if the cron stops; logs kept 7 days.
- Free Supabase: pauses after 7 days with no activity (the cron keeps it active).
