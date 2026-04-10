#!/usr/bin/env python3

from s3rn import S3RN, S3RemoteFolder, S3RemoteDocument, S3RemoteCanvas, S3RemoteFile


RELAY = "11111111-1111-1111-1111-111111111111"
FOLDER = "22222222-2222-2222-2222-222222222222"
DOC = "33333333-3333-3333-3333-333333333333"


def test_compound_id_folder():
    r = S3RemoteFolder(RELAY, FOLDER)
    assert S3RN.get_compound_document_id(r) == f"{RELAY}-{FOLDER}"


def test_compound_id_document():
    r = S3RemoteDocument(RELAY, FOLDER, DOC)
    assert S3RN.get_compound_document_id(r) == f"{RELAY}-{DOC}"


def test_compound_id_canvas():
    r = S3RemoteCanvas(RELAY, FOLDER, DOC)
    assert S3RN.get_compound_document_id(r) == f"{RELAY}-{DOC}"


def test_compound_id_file_includes_folder():
    """Files use 3-UUID format: relay_id-folder_id-file_id"""
    r = S3RemoteFile(RELAY, FOLDER, DOC)
    assert S3RN.get_compound_document_id(r) == f"{RELAY}-{FOLDER}-{DOC}"
