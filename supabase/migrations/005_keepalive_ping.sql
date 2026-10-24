-- Keeps the free Render server awake: Supabase pings /health every 10 minutes.
-- (Render's free plan sleeps after 15 min without traffic; waking takes 30-60 s.)
-- Replace the URL if the backend moves. Stop with: select cron.unschedule('spotter-keepalive');
create extension if not exists pg_cron;
create extension if not exists pg_net;
select cron.unschedule('spotter-keepalive') where exists (select 1 from cron.job where jobname = 'spotter-keepalive');
select cron.schedule('spotter-keepalive', '*/10 * * * *',
  $$ select net.http_get(url := 'https://spotter-backend-qdw9.onrender.com/health', timeout_milliseconds := 60000) $$);
