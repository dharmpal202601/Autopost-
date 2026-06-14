"""
app/scheduler.py
Publish delay scheduler.
Reads the configured delay from APP_CONFIG and computes the 'not before' timestamp
for each post entering the queue.
"""

from datetime import datetime, timedelta

from app.config import APP_CONFIG


class PostScheduler:
    """Computes publish times based on the configured delay setting."""

    def get_delay_seconds(self) -> int:
        """Return the configured publish delay in seconds."""
        return APP_CONFIG.get_publish_delay()

    def compute_publish_time(self) -> datetime:
        """
        Return the earliest UTC datetime at which a post should be published.
        If delay is 0, returns utcnow() (immediate).
        """
        delay = self.get_delay_seconds()
        return datetime.utcnow() + timedelta(seconds=delay)

    def is_due(self, delay_until: datetime) -> bool:
        """Return True if it's time to publish (delay_until has passed)."""
        if delay_until is None:
            return True
        return datetime.utcnow() >= delay_until

    def compute_catchup_publish_time(self) -> datetime:
        """
        Assigns the next available catch-up slot for missed posts.
        Each missed post gets scheduled 2 minutes after the previous one.
        Starts from now if no pending catch-up posts exist.

        Used when the monitor restarts and finds images that were missed
        while it was offline — uploads them with 2-minute gaps.
        """
        from app.database import SessionLocal, Post
        from sqlalchemy import func

        session = SessionLocal()
        try:
            # Find the latest scheduled catch-up or pending post time
            max_delay = session.query(func.max(Post.delay_until)).filter(
                Post.status.in_(["pending", "processing"]),
                Post.post_type == "catchup",
            ).scalar()
        finally:
            session.close()

        now = datetime.utcnow()

        if max_delay and max_delay > now:
            # Schedule 2 minutes after the last pending catch-up post
            return max_delay + timedelta(minutes=2)
        else:
            # No pending catch-up posts — start from now
            return now

    def compute_historical_publish_time(self) -> datetime:
        """
        Assigns the next available historical slot: 4:00, 13:00, or 19:00 (Local Time).
        We calculate this by querying the max scheduled time in the DB.
        """
        from app.database import SessionLocal, Post
        from sqlalchemy import func
        from datetime import date, time

        session = SessionLocal()
        try:
            # Find the latest scheduled historical post
            max_delay = session.query(func.max(Post.delay_until)).filter_by(post_type="historical").scalar()
        finally:
            session.close()

        now = datetime.utcnow()
        # For simplicity, we just use UTC for calculating the hour offsets.
        # Actually, user wants IST (local time). So let's calculate based on local time.
        # But our DB stores UTC.
        # Let's just calculate in local time, then convert to UTC.
        from datetime import timezone
        import time as time_mod
        
        # Get current local time
        local_now = datetime.fromtimestamp(time_mod.time())
        
        if max_delay:
            # max_delay is UTC. Convert to local for slot calculation.
            max_delay_local = datetime.fromtimestamp(max_delay.replace(tzinfo=timezone.utc).timestamp())
            base_time = max(local_now, max_delay_local)
        else:
            base_time = local_now
            
        # Find next slot after base_time
        # Slots: 04:00, 13:00, 19:00
        slots = [4, 13, 19]
        
        current_hour = base_time.hour
        next_slot_hour = None
        days_ahead = 0
        
        for slot in slots:
            if current_hour < slot:
                next_slot_hour = slot
                break
                
        if next_slot_hour is None:
            # Next day's first slot
            next_slot_hour = slots[0]
            days_ahead = 1
            
        next_local = base_time.replace(hour=next_slot_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
        
        # Convert next_local back to UTC
        # The difference between UTC and local:
        local_timestamp = next_local.timestamp()
        utc_datetime = datetime.utcfromtimestamp(local_timestamp)
        return utc_datetime


# Module-level singleton
post_scheduler = PostScheduler()
