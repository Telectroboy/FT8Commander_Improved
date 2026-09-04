import unittest

from decode_normalizer import normalize_message_segments


class TestDecodeNormalizer(unittest.TestCase):
    def test_observed_mshv_multi_answer_message(self):
        self.assertEqual(
            normalize_message_segments(
                "F4EGM RR73; JA1MLV <CN8NS> -08"
            ),
            ["F4EGM CN8NS RR73", "JA1MLV <CN8NS> -08"],
        )

    def test_documented_generic_mshv_form(self):
        self.assertEqual(
            normalize_message_segments("A2AA RR73; B2BB <C2CC> +05"),
            ["A2AA C2CC RR73", "B2BB <C2CC> +05"],
        )

    def test_standard_directed_message_is_unchanged(self):
        self.assertEqual(
            normalize_message_segments("F4EGM CN8NS RR73"),
            ["F4EGM CN8NS RR73"],
        )

    def test_non_bracketed_sender_is_not_inferred(self):
        self.assertEqual(
            normalize_message_segments("F4EGM RR73; JA1MLV CN8NS -08"),
            ["F4EGM RR73", "JA1MLV CN8NS -08"],
        )

    def test_non_terminal_short_segment_is_not_inferred(self):
        self.assertEqual(
            normalize_message_segments("F4EGM -08; JA1MLV <CN8NS> -08"),
            ["F4EGM -08", "JA1MLV <CN8NS> -08"],
        )

    def test_other_segments_are_preserved(self):
        self.assertEqual(
            normalize_message_segments(
                "CQ K1ABC FN31; F4EGM 73; JA1MLV <CN8NS> -08"
            ),
            ["CQ K1ABC FN31", "F4EGM CN8NS 73", "JA1MLV <CN8NS> -08"],
        )


if __name__ == "__main__":
    unittest.main()
