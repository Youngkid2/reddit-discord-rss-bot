import unittest

from channel_map import (
    is_ignored_channel,
    parse_csv_names,
    parse_override_map,
    subreddit_candidates,
)


class ChannelMapTests(unittest.TestCase):
    def test_case_insensitive_as_is_and_underscore(self):
        self.assertEqual(subreddit_candidates("xyz"), ["xyz"])
        self.assertEqual(subreddit_candidates("Some-Name"), ["Some-Name", "Some_Name"])
        self.assertEqual(subreddit_candidates("already_ok"), ["already_ok"])

    def test_override_wins(self):
        overrides = parse_override_map("cute-pics:aww, Foo:Bar_Sub")
        self.assertEqual(overrides["cute-pics"], "aww")
        self.assertEqual(overrides["foo"], "Bar_Sub")
        self.assertEqual(
            subreddit_candidates("cute-pics", overrides),
            ["aww"],
        )

    def test_ignore_list_unless_overridden(self):
        ignore = parse_csv_names("general, rules", defaults=())
        overrides = parse_override_map("general:something")
        self.assertTrue(is_ignored_channel("General", ignore, {}))
        self.assertFalse(is_ignored_channel("general", ignore, overrides))
        self.assertFalse(is_ignored_channel("xyz", ignore, {}))


if __name__ == "__main__":
    unittest.main()
