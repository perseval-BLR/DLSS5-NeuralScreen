# Optional DLSS library updates

NeuralScreen makes no update requests by default. In Settings, enable
**Check library updates automatically** to contact NVIDIA's GitHub repository
at startup. The switch is saved as `library_updates_enabled: true`.
**Check official versions** explicitly performs a one-time check without enabling
automatic startup checks. Turning the switch off disables future automatic checks.

The startup notification has Update and Close buttons. Updates are downloaded
only after an explicit click, verified against the official repository blob hash
and the expected DLL version, then staged for the next launch. Installed DLLs
are backed up as `.bak`. Running libraries are never replaced live.

The official source currently publishes SR and FG libraries. NR has no public
update source here; the checker does not claim its local version is current.
Network failures leave processing available and report an unknown version.

`runtime/python.exe tests/test_library_updates.py` runs entirely offline:
socket connections are forbidden by the test fixture; network responses are mocked.
