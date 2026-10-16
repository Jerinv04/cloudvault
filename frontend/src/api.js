import { getIdToken } from './auth.js';

const API = (import.meta.env.VITE_API_URL || '').replace(/\/$/, '');

// Browsers sometimes leave file.type empty (e.g. .md files) - infer it.
const EXT_TYPES = {
  md: 'text/markdown', txt: 'text/plain', csv: 'text/csv', json: 'application/json',
  pdf: 'application/pdf', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
  gif: 'image/gif', webp: 'image/webp', zip: 'application/zip',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
};
export const contentTypeOf = (file) =>
  file.type || EXT_TYPES[file.name.split('.').pop().toLowerCase()] || 'application/octet-stream';

async function call(path, { method = 'GET', body } = {}) {
  const token = await getIdToken();
  if (!token) throw new Error('Your session expired - please sign in again.');
  const res = await fetch(`${API}${path}`, {
    method,
    headers: { Authorization: token, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `Request failed (${res.status})`);
  return data;
}

export const listFiles = (tag = '') => call(`/files${tag ? `?tag=${encodeURIComponent(tag)}` : ''}`);
export const shareFile = (fileId, expiresIn) => call(`/files/${fileId}/share`, { method: 'POST', body: { expiresIn } });
export const retagFile = (fileId) => call(`/files/${fileId}/tag`, { method: 'POST' });

// 1) ask uploadFile Lambda for a pre-signed POST, 2) send the bytes straight to S3.
export async function uploadFile(file) {
  const { fileId, upload } = await call('/files/upload-url', {
    method: 'POST',
    body: { fileName: file.name, contentType: contentTypeOf(file), size: file.size },
  });
  const form = new FormData();
  Object.entries(upload.fields).forEach(([k, v]) => form.append(k, v));
  form.append('file', file); // must be the last field
  const res = await fetch(upload.url, { method: 'POST', body: form });
  if (!res.ok) throw new Error(`S3 rejected the upload (${res.status})`);
  return fileId;
}
