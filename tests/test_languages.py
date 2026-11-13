import pycountry

from baycone.languages import ALPHA_3_TO_ALPHA_2, BIBLIOGRAPHIC_TO_TERMINOLOGY


def test_bibliographic_table_matches_pycountry():
    expected = {lang.bibliographic: lang.alpha_3 for lang in pycountry.languages if hasattr(lang, "bibliographic")}
    assert BIBLIOGRAPHIC_TO_TERMINOLOGY == expected


def test_alpha_2_table_matches_pycountry():
    expected = {lang.alpha_3: lang.alpha_2 for lang in pycountry.languages if hasattr(lang, "alpha_2")}
    assert ALPHA_3_TO_ALPHA_2 == expected
