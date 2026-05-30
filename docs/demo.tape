# vhs script — records examples/01_stdout/demo.py as docs/demo.gif
# for the top of README. Regenerate with:
#
#   brew install vhs   # or: go install github.com/charmbracelet/vhs@latest
#   vhs docs/demo.tape
#
# Output: docs/demo.gif (commit it).

Output docs/demo.gif

Set Shell "bash"
Set FontSize 13
Set Width 1200
Set Height 720
Set Theme "Dracula"
Set TypingSpeed 60ms
Set PlaybackSpeed 1.0

# Hide the prompt-setup and venv-activation keystrokes from the recording.
Hide
Type "source .venv/bin/activate"
Enter
Type "export PS1='$ '"
Enter
Type "clear"
Enter
Show

# Comment framing what the viewer is about to see.
Type "# Baton — see what an MCP agent is actually doing, in 10 seconds."
Sleep 800ms
Enter
Type "# Zero config: stdout sink, no backend, no auth."
Sleep 800ms
Enter
Enter

# Run the demo. JSON Lines on stderr → jq strips envelope noise so each event
# fits on one readable line (full envelope shape is in SPEC §11.4).
Type "python examples/01_stdout/demo.py 2>&1 | jq -c '{seq: .sequence_number, type: .event_type, payload}'"
Sleep 500ms
Enter

# Give the viewer time to absorb the event stream.
Sleep 9s

Enter
Type "# 7 events: 3 per logical trace + 1 reactive 'dead_end' friction signal."
Sleep 1500ms
Enter
Type "# Same envelope (SPEC §11.4) ships to any sink — stdout, file, HTTP, fan-out."
Sleep 3s
Enter
