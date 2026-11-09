"""The mpv command line must keep the audio device open across tracks."""


def test_mpv_args_are_gapless_and_cached():
    from pandora.pandora_player import _mpv_args

    args = _mpv_args("mpv.exe", volume=70, pipe=r"\\.\pipe\pandora")

    assert "--gapless-audio=yes" in args
    assert "--gapless-audio=no" not in args
    assert "--cache=yes" in args
    assert "--cache-secs=30" in args
    assert "--volume=70" in args
    assert "--no-video" in args
    assert args[0] == "mpv.exe"
