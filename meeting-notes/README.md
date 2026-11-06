# Meeting Notes (free, local)

Records a Teams or Zoom call on your Windows laptop and, when the call ends, writes a transcript and summary notes.
There's nothing extra to pay: transcription runs offline with Whisper, and the summary uses the Claude Code you already have.

## Use it

1. Join the Teams or Zoom call. Tell the attendees you are recording.
2. Double-click `start.bat` and type a meeting name.
3. When the call ends, press **Enter** in that window.
4. Open `meetings\<date>_<name>\notes.md`.

Each meeting folder contains:

| File | What it is |
|------|------------|
| `system.wav` | call audio (the other people) |
| `mic.wav` | your microphone |
| `transcript.md` | timestamped transcript labelled **Me** / **Others** |
| `notes.md` | summary, decisions, action items, open questions |

## Options

```
python meeting_notes.py --model medium              more accurate, slower (default: small)
python meeting_notes.py --language auto             auto-detect the spoken language (default: en)
python meeting_notes.py --process meetings\<folder> redo the transcript and notes for an earlier recording
python meeting_notes.py --list-devices              list the audio devices
```

## Tips

- Use your headset. With laptop speakers your mic also picks up the other people, so their lines appear twice.
- The call audio is captured from your **default** playback device. Set Teams or Zoom to output to that device.
- On first use the script downloads the Whisper model (~250 MB for `small`) from huggingface.co. If the office network blocks that site, do the first run on another network. The model is cached after that.
- Audio files take about 350 MB per hour per track. Delete the `.wav` files once you have the notes.
