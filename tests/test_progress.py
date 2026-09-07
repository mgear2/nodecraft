import io

from lib.progress import Progress


def test_progress_reports_stages_and_items():
    output = io.StringIO()
    progress = Progress(stream=output)

    progress.stage("scanning")
    items = progress.items("files", 2)
    items.update(bytes_count=1024, is_file=True)
    items.update(bytes_count=2048, is_file=True)
    items.finish()

    text = output.getvalue()
    assert "[nodecraft] scanning" in text
    assert "files: 2/2, 2 files, 3.0 KB" in text


def test_quiet_progress_is_silent():
    output = io.StringIO()
    progress = Progress(quiet=True, stream=output)

    progress.stage("scanning")
    items = progress.items("files", 1)
    items.update()
    items.finish()

    assert output.getvalue() == ""
