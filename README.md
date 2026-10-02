# Candle Desk media

- **main** branch: shared tools for the daily tasks.
  - `pron.json`: how the voice engine should say crypto terms (word -> respelling). Both the morning news and the evening lessons use it. Add a line whenever a word is mispronounced.
  - `check_script.py`: run on each day's script before voicing; lists unusual words with their pronunciation plus digits, long captions and long lines to fix.
- **media** branch: temporary video files for scheduled posts, replaced daily and removed after posting.
