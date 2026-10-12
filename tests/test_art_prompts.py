"""art/prompts.yaml: structure, seeds and wording rules (plan M3 task 03)."""

import re
from pathlib import Path

import yaml

from dndmusic.starter import STARTER_SCENES

ART = yaml.safe_load((Path(__file__).parent.parent / "art" / "prompts.yaml").read_text())
DENY = {"text", "people", "man", "woman", "person", "men", "women"}


def test_people_sets_exist_and_icons_cover_every_scene():
    assert set(ART["people"]) <= set(ART["sets"])
    assert set(ART["icons"]) == set(ART["sets"])
    assert len({i["seed"] for i in ART["icons"].values()}) == 18
    assert not {i["seed"] for i in ART["extra_icons"].values()} & {i["seed"] for i in ART["icons"].values()}


def _all():
    for slug, items in {**ART["sets"], **ART["extra_sets"]}.items():
        for item in items:
            yield slug, item
    for item in ART["generic"]:
        yield "generic", item


def test_every_starter_slug_has_prompts_and_no_extras():
    slugs = [s.get("icon", s["slug"]) for s in STARTER_SCENES]   # a scene may borrow another's art
    assert set(ART["sets"]) == set(slugs)
    assert all(len(ART["sets"][s]) >= 4 for s in slugs)


def test_extra_sets_have_icons_and_four_prompts():
    assert not set(ART["extra_sets"]) & set(ART["sets"])
    assert set(ART["extra_icons"]) == set(ART["extra_sets"])
    assert all(len(v) == 4 for v in ART["extra_sets"].values())
    assert set(ART["extra_people"]) <= set(ART["extra_sets"])


def test_generic_has_twelve():
    assert len(ART["generic"]) == 12


def test_style_and_negative_present():
    assert ART["style"].strip() and ART["negative"].strip()


def test_seeds_unique_ints():
    seeds = [item["seed"] for _, item in _all()]
    assert all(isinstance(s, int) for s in seeds)
    assert len(seeds) == len(set(seeds))


def test_prompt_length_and_deny_words():
    for slug, item in _all():
        words = re.findall(r"[a-z']+", item["prompt"].lower())
        assert len(words) <= 40, (slug, item)
        assert "text" not in words, (slug, item)
        if slug not in ART["people"] + ART["extra_people"]:
            assert not DENY & set(words), (slug, item)
