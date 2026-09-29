# Tiny turn detector (end of utterance, text only)

Given the transcript so far (ASR-style: lowercase, no punctuation), estimate P(the speaker has finished). Cedar (`policies/turn.cedar`) then decides
`Respond` / `Backchannel` / `Wait` from that probability plus silence duration, voice activity and an independent "dangling word" rule.

* Model: hashed word n-gram features (last 1-3 words, first words, length, 6-word window uni/bigrams) -> logistic regression. 65,536 weights (`turn_detector.json`, 536 KB), pure-Python inference, ~7 us per call on this box.
* **Audio is not used.** Silence duration is an input from whatever front end you have; this model never hears speech, so prosody (falling pitch, filler sounds) is invisible to it.
* Training data: DailyDialog (`roskoN/dailydialog`; the original card says cc-by-nc-sa-4.0, so **research use only**; the data is not committed, the weights derived from it are). Turns are written dialogue, not transcribed speech.
* Labels are **constructed**, not annotated: complete = a whole turn; incomplete = a word-prefix of a turn. Noise controls in `train.py`: prefixes that are themselves whole turns elsewhere in the corpus are dropped; prefixes ending at a sentence boundary inside a multi-sentence turn are dropped.

## Results (`python evaluate.py`, numbers in `eval.json`)

| set | model acc | trivial rule baseline acc |
|---|--:|--:|
| constructed held-out test (21,077 rows, same construction as training) | 0.837 | 0.617 |
| 80 hand-written probes (written by the assistant, clear-cut; NOT third-party annotated) | 0.900 | 0.800 |

The probes are tiny (80): a difference of 2-3 items is within noise. At threshold 0.5 the model interrupts (calls an unfinished turn complete) on 1/40 probes and waits too long on 7/40.
It misses short complete utterances that end in a verb or noun after a modal ("i think we should leave now", "that will be all"); the sample errors are listed by the script.
**Not measured:** accuracy on real ASR transcripts, real conversations with human turn annotations, other languages, or any latency inside a live audio pipeline. The 0.837 is for the constructed task only and should not be read as real-world end-of-turn accuracy.

## Cedar side
`turn-properties` (hard check): 576 boundary combinations against an independent recomputation, never talks over speech, monotone in silence and probability. 14 golden cases. Parameters in `policies/params.json` (`turn_min_p` 70, `turn_min_silence_ms` 300, `turn_max_wait_ms` 1500, `turn_backchannel_p` 30) are my defaults, not tuned on data.

```
python turn/train.py /path/to/dailydialog   # needs numpy, scipy, scikit-learn
python turn/evaluate.py
python turn/demo.py "i'd like to book a table for two tonight" 600
findajev turn-decide --p 55 --silence 400 [--speaking] [--dangling]
```
