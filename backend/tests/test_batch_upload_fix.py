"""Phase 3 / DATA-8: /api/upload/batch used to fail on every batch (a processing step was logged against a
group id that has no images row, a foreign-key error). Steps are now logged against the real image request ids."""

import asyncio
import io
import json
import unittest

from PIL import Image as PILImage
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers, UploadFile

import app.models  # noqa: F401 -- register every model with Base.metadata
from app.api.routes.batch_upload import upload_images_batch
from app.database import Base
from app.models.image import Image
from app.models.processing_log import ProcessingLog
from tests.db_support import make_engine


def _jpeg(color):
    buf = io.BytesIO()
    PILImage.new("RGB", (40, 40), color).save(buf, format="JPEG")
    return buf.getvalue()


def _upload(name, data):
    return UploadFile(file=io.BytesIO(data), filename=name, headers=Headers({"content-type": "image/jpeg"}))


class BatchUploadTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def run_batch(self, files):
        return asyncio.run(upload_images_batch(files=files, db=self.db))

    def test_a_batch_of_three_is_stored_and_logged_against_the_real_images(self):
        response = self.run_batch([_upload(f"p{i}.jpg", _jpeg((i * 40, 10, 10))) for i in range(3)])
        self.assertEqual(response.count, 3)
        self.assertEqual([i.order for i in response.images], [0, 1, 2])
        images = self.db.query(Image).all()
        self.assertEqual(len(images), 3)
        steps = self.db.query(ProcessingLog).filter(ProcessingLog.processing_step == "batch_upload").all()
        self.assertEqual(len(steps), 3)
        image_request_ids = {i.request_id for i in images}
        self.assertEqual({s.request_id for s in steps}, image_request_ids)       # every step points at a real image
        for step in steps:
            self.assertEqual(json.loads(step.metadata_json)["group_id"], response.group_id)

    def test_a_file_that_cannot_be_stored_is_reported_and_the_rest_still_upload(self):
        response = self.run_batch([_upload("ok.jpg", _jpeg((1, 2, 3))), _upload("bad.exe", b"MZ....")])
        self.assertEqual(response.count, 1)
        self.assertEqual(self.db.query(Image).count(), 1)

    def test_a_batch_with_nothing_valid_is_a_400(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            self.run_batch([_upload("bad.exe", b"MZ....")])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_too_many_files_and_no_files_are_refused(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            self.run_batch([])
        with self.assertRaises(HTTPException):
            self.run_batch([_upload(f"p{i}.jpg", _jpeg((1, 1, 1))) for i in range(21)])


if __name__ == "__main__":
    unittest.main()
