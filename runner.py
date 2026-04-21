import time
from datetime import datetime, timezone, timedelta
from main import run_job

def seconds_until_next_run(hour=0, minute=10):
    """Calculate exact seconds until the next 00:10 UTC."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

# Run immediately on deploy
print("Running on startup...")
run_job()

# Sleep precisely until 00:10 UTC, then run daily
while True:
    wait = seconds_until_next_run()
    next_run = datetime.now(timezone.utc) + timedelta(seconds=wait)
    print(f"Sleeping {wait/3600:.2f}h -- next run at {next_run.strftime('%Y-%m-%d %H:%M UTC')}")
    time.sleep(wait)
    run_job()
    # After running, sleep 24h to avoid re-triggering within the same minute
    time.sleep(60)
