# Free-text scenes (M4)

The DM types a sentence in "Describe a scene" (Scenes tab). `POST /api/scene-from-text` lowercases it, strips apostrophes ("dragon's" -> "dragons") and matches whole words against ordered rule lists; the first rule whose words appear wins, so put specific phrases first. Location, mood, tempo, choir and start (straight in for intense scenes) are picked, the DM's words go to `extra`, and the result comes back as an unsaved draft that opens in the editor. Nothing is stored or generated until the DM saves; music is generated when the scene is played, so describing needs no GPU. Unmatched text gives location `bardcore`, mood `calm`, tempo `medium`.

## Rules file

- Default: `dndmusic/freetext_default.yaml` (ships with the package).
- Yours: `<data_dir>/freetext.yaml`, copied from the default on first start. Edit that copy and restart the conductor. A bad file stops startup with a message naming the problem.
- Targets (`location:`, `mood:`) must be keys of `vocab.yaml`; rules pointing at a missing key are skipped.
- Sections: `locations`, `moods`, `tempos` (ordered `when: [words] -> target`), `mood_tempo`, `choir` words and `choir_moods`, `straight_moods` and `straight_words`, `defaults`, `extra_max_words` (1-20), `extra_max_chars` (1-200). Rule words are matched after the same cleaning as the DM's text (quotes dropped, letters and digits only), so "dragon's" and "dragons" are the same word; accents are not folded.

Example: add "lighthouse" as an at-sea place and "spooky" as dread. Put the location rule before any rule it should beat:

```yaml
locations:
  - {when: [lighthouse, light house, shipwreck], location: at sea}
  # ... existing rules ...
moods:
  - {when: [spooky, creepy], mood: dread}
  # ... existing rules ...
```

Multi-word entries match as a phrase ("light house").

## The extra-words cap

At most 9 words / 120 chars of the DM's text are kept in `extra` (`extra_max_words`); the scene name is the same text cut to 40 chars. `scene_prompt` trims vocab sentences from the end to stay under 70 words, so more extra words push out the mood sentence. Known limitation: on long locations (tavern, bardcore, at sea, forest road, town, city) a straight-in prompt can lose the mood sentence to the 70-word cap at any setting; that is the prompt trimming, not the rules.

## Art

`art_for(scene)` in `dndmusic/starter.py` picks a starter set: the scene name if it is one, else the starter scene with the same location and mood, else one with the same location, else `None` (generic). An unsaved Custom scene gets the matched art too. The API returns it as `art`; `bg.js` `setFor(manifest, name, art)` uses own set, then `art`, then generic.

## An LLM version

`text_to_scene(text, vocab, rules=None) -> Scene` in `dndmusic/freetext.py` is the seam. Replace the body only: return a `Scene` with `saved=False`, vocab keys for location and mood (`vocab.validate(scene)` is called at the end). The endpoint, editor, art choice and guardrails (they live in `scene_prompt`) stay as they are.

## Testing edits

`.venv/bin/pytest -q tests/test_freetext.py`; `tests/test_packaging.py` checks the default file ships.
