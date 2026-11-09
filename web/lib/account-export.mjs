/** Validate the server format before offering a download/success notification. */
export async function validateAccountExport(blob) {
  if (!(blob instanceof Blob) || blob.size > 32 * 1024 * 1024) {
    throw new Error('账号导出文件无效或超过 32 MiB');
  }
  const kind = blob.type.split(';')[0].trim().toLowerCase();
  if (kind === 'application/json') {
    const data = JSON.parse(await blob.text());
    if (!Array.isArray(data) || data.some(item => !item || typeof item !== 'object'
        || typeof item.platform !== 'string' || typeof item.uid !== 'string')) {
      throw new Error('服务器未返回有效的账号清单');
    }
    return {blob, filename: 'accounts.json', format: 'manifest'};
  }
  if (!['application/zip', 'application/x-zip-compressed', 'application/octet-stream'].includes(kind)) {
    throw new Error('服务器返回了不支持的导出格式');
  }
  const data = new Uint8Array(await blob.arrayBuffer());
  const view = new DataView(data.buffer);
  const u16 = p => view.getUint16(p, true);
  const u32 = p => view.getUint32(p, true);
  if (data.length < 22) throw new Error('账号 ZIP 导出文件不完整');
  let end = -1;
  for (let p = data.length - 22; p >= Math.max(0, data.length - 65557); p--) {
    if (u32(p) === 0x06054b50 && p + 22 + u16(p + 20) === data.length) {end = p; break;}
  }
  if (end < 0 || u16(end + 4) || u16(end + 6) || u16(end + 8) !== u16(end + 10)
      || u16(end + 10) > 20000 || u32(end + 16) + u32(end + 12) !== end) {
    throw new Error('账号 ZIP 中央目录校验失败');
  }
  let cursor = u32(end + 16);
  for (let i = 0; i < u16(end + 10); i++) {
    if (cursor + 46 > end || u32(cursor) !== 0x02014b50) throw new Error('账号 ZIP 目录不完整');
    const local = u32(cursor + 42);
    if (local + 30 > u32(end + 16) || u32(local) !== 0x04034b50
        || local + 30 + u16(local + 26) + u16(local + 28) + u32(cursor + 20) > u32(end + 16)) {
      throw new Error('账号 ZIP 内容不完整');
    }
    cursor += 46 + u16(cursor + 28) + u16(cursor + 30) + u16(cursor + 32);
  }
  if (cursor !== end) throw new Error('账号 ZIP 目录长度不一致');
  return {blob, filename: 'accounts.zip', format: 'zip'};
}
