"""Host-configured document selections for individual approved applications."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from types import MappingProxyType
import uuid

from store import ReviewError


FIELDS = frozenset({'path', 'sha256', 'bytes', 'uploaded_filename', 'source_reference'})


class ApplicationResumeChanged(ReviewError):
    def __init__(self):
        super().__init__('The selected application resume no longer matches its approved file.',
                         409, reason='application_resume_changed')


@dataclass(frozen=True)
class ResumeSelection:
    process_id: str
    path: Path
    sha256: str
    bytes: int
    uploaded_filename: str
    source_reference: str

    def assert_current(self):
        """Check the actual bytes, not the name, timestamp, or a saved receipt."""
        try:
            if not self.path.is_file() or self.path.stat().st_size != self.bytes:
                raise ApplicationResumeChanged()
            digest, count = hashlib.sha256(), 0
            with self.path.open('rb') as stream:
                while True:
                    chunk = stream.read(min(1024 * 1024, self.bytes - count + 1))
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > self.bytes:
                        raise ApplicationResumeChanged()
                    digest.update(chunk)
            if count != self.bytes or digest.hexdigest() != self.sha256:
                raise ApplicationResumeChanged()
        except OSError:
            raise ApplicationResumeChanged() from None

    def provenance(self):
        return {'process_id': self.process_id, 'sha256': self.sha256, 'bytes': self.bytes,
                'uploaded_filename': self.uploaded_filename, 'source_reference': self.source_reference}

    def matches_upload(self, proof):
        return (isinstance(proof, dict) and proof.get('sha256') == self.sha256
                and type(proof.get('bytes')) is int and proof['bytes'] == self.bytes)


def load_selections(value, *, base_directory=None):
    """Validate and copy host settings; task data never calls this loader."""
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, dict):
        raise ValueError('Application resumes must be a process-specific mapping.')
    selections = {}
    for process_id, item in value.items():
        try:
            if not isinstance(process_id, str) or str(uuid.UUID(process_id)) != process_id:
                raise ValueError()
            if not isinstance(item, dict) or set(item) != FIELDS:
                raise ValueError()
            if (not isinstance(item['path'], str) or not item['path'].strip() or '\x00' in item['path']
                    or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])
                    or type(item['bytes']) is not int or item['bytes'] <= 0):
                raise ValueError()
            filename, reference = item['uploaded_filename'], item['source_reference']
            if (not isinstance(filename, str) or not filename.strip() or len(filename) > 255
                    or filename in {'.', '..'} or any(c in filename for c in ('/', '\\', '\x00', '\n', '\r'))
                    or not isinstance(reference, str) or not reference.strip() or len(reference) > 2000
                    or '\x00' in reference):
                raise ValueError()
            path = Path(item['path']).expanduser()
            if not path.is_absolute():
                path = Path(base_directory) / path if base_directory is not None else path
            selection = ResumeSelection(process_id, path.resolve(), item['sha256'], item['bytes'], filename, reference)
            selection.assert_current()
        except (ValueError, TypeError, OSError, ApplicationResumeChanged):
            raise ValueError('An application resume selection is invalid or does not match its file.') from None
        selections[process_id] = selection
    return MappingProxyType(selections)
