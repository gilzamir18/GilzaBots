import json

from gilzabots.ide_server import (
    _PDF_EXPORT_RESULT_EVENT,
    _parse_pdf_export_frame_name,
    _pdf_export_result_script,
)


def test_pdf_frame_protocol_decodes_the_request_and_suggested_name():
    assert _parse_pdf_export_frame_name(
        "gilzabots-pdf-v1:m1-2:Aula%2002%3A%20vetores.pdf"
    ) == ("m1-2", "Aula 02: vetores.pdf")


def test_unrelated_or_malformed_frames_do_not_enter_the_export_protocol():
    assert _parse_pdf_export_frame_name("ordinary-frame") is None
    assert _parse_pdf_export_frame_name("gilzabots-pdf-v1:no-name-separator") is None
    assert _parse_pdf_export_frame_name("gilzabots-pdf-v1:bad.id:file.pdf") is None


def test_pdf_result_notification_is_json_encoded_for_javascript():
    message = 'Disk "full"\nTry another folder.'
    script = _pdf_export_result_script("request-7", "failed", message)

    assert json.dumps(_PDF_EXPORT_RESULT_EVENT) in script
    assert json.dumps(message, ensure_ascii=False) in script
    assert "request-7" in script
    assert "new CustomEvent" in script

