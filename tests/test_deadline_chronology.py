"""Deadline resolution across daylight-saving transitions and UTC/local date boundaries (tasks 193-194).
Expected instants are written by hand from the IANA rules, not computed by the code under test."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from inbox_intel import resolve_deadline_detail


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


class DaylightSaving(unittest.TestCase):
    def test_tomorrow_at_a_clock_time_across_the_autumn_change(self):
        # Berlin leaves CEST (UTC+2) for CET (UTC+1) on 2026-10-25 at 03:00 local.
        sent = utc(2026, 10, 24, 8, 0)                                   # Sat 10:00 CEST
        d = resolve_deadline_detail('Could you send it by tomorrow 5 pm?', sent, 'Europe/Berlin')
        self.assertEqual(d['deadline_at'], utc(2026, 10, 25, 16, 0))     # Sun 17:00 CET
        self.assertEqual(d['phrase'], 'tomorrow 5 pm')

    def test_end_of_day_across_the_spring_change(self):
        # New York enters EDT (UTC-4) on 2026-03-08 at 02:00 local.
        sent = utc(2026, 3, 7, 15, 0)                                    # Sat 10:00 EST
        d = resolve_deadline_detail('Please review it by tomorrow.', sent, 'America/New_York')
        self.assertEqual(d['deadline_at'], utc(2026, 3, 9, 3, 59, 59))   # Sun 23:59:59 EDT

    def test_weekday_deadline_spanning_the_change_keeps_local_wall_time(self):
        sent = utc(2026, 10, 23, 7, 0)                                   # Fri 09:00 CEST
        d = resolve_deadline_detail('Send the report by Monday 9 am.', sent, 'Europe/Berlin')
        self.assertEqual(d['deadline_at'], utc(2026, 10, 26, 8, 0))      # Mon 09:00 CET


class DateBoundaries(unittest.TestCase):
    def test_today_is_the_senders_local_day_not_the_utc_day(self):
        sent = utc(2026, 11, 1, 2, 0)                                    # 2026-10-31 19:00 in Los Angeles (PDT)
        d = resolve_deadline_detail('I need this today.', sent, 'America/Los_Angeles')
        self.assertEqual(d['deadline_at'], utc(2026, 11, 1, 6, 59, 59))  # Oct 31 23:59:59 PDT

    def test_tomorrow_ahead_of_utc(self):
        sent = utc(2026, 10, 30, 23, 30)                                 # 2026-10-31 08:30 in Tokyo
        d = resolve_deadline_detail('Could you reply by tomorrow?', sent, 'Asia/Tokyo')
        self.assertEqual(d['deadline_at'], utc(2026, 11, 1, 14, 59, 59)) # Nov 1 23:59:59 JST

    def test_same_message_gives_different_instants_in_different_zones(self):
        sent = utc(2026, 6, 1, 12, 0)
        a = resolve_deadline_detail('by tomorrow', sent, 'UTC')['deadline_at']
        b = resolve_deadline_detail('by tomorrow', sent, 'Asia/Kolkata')['deadline_at']
        self.assertEqual(a - b, 5.5 * 3600)

    def test_vague_phrases_stay_unresolved(self):
        for text in ('Please send it soon.', 'When possible, take a look.'):
            d = resolve_deadline_detail(text, utc(2026, 6, 1, 12, 0), 'Europe/Berlin')
            self.assertIsNone(d['deadline_at'], text)
            self.assertEqual(d['deadline_confidence'], 'AMBIGUOUS', text)


if __name__ == '__main__':
    unittest.main()
