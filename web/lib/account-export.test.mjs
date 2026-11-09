import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validateAccountExport} from './account-export.mjs';

test('metadata is explicitly a JSON inventory', async () => {
  const result = await validateAccountExport(new Blob([JSON.stringify([{platform:'qoder',uid:'synthetic'}])], {type:'application/json'}));
  assert.equal(result.filename,'accounts.json');
  assert.equal(result.format,'manifest');
});
test('shadowed empty object and JSON pretending to be ZIP are rejected', async () => {
  for (const type of ['application/json','application/zip','text/html']) {
    await assert.rejects(validateAccountExport(new Blob(['{}'],{type})));
  }
});
test('complete empty ZIP is accepted, truncated ZIP is rejected', async () => {
  const bytes = new Uint8Array(22);bytes.set([0x50,0x4b,5,6]);
  assert.equal((await validateAccountExport(new Blob([bytes],{type:'application/zip'}))).format,'zip');
  await assert.rejects(validateAccountExport(new Blob([bytes.slice(0,20)],{type:'application/zip'})));
});
