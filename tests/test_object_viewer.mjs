// node --loader ./tests/three_loader.mjs --test tests/test_object_viewer.mjs
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import * as THREE from 'three';
import {ObjectViewer} from '../gui/object_viewer.js';

test('mixed OBJ faces and loose lines retain Akatsuki surface meshes', async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async () => { requests++; return new Response(await readFile(new URL('../models/akatsuki.obj', import.meta.url))); };
  try {
    const object = await ObjectViewer.prototype.readModel.call({}, {url: '/model-assets/akatsuki.obj', parts: {PLANET_C: {}}});
    for (const name of ['PLANET-C_v24', 'sap1', 'sap2']) assert.equal(object.getObjectByName(name)?.isMesh, true, name);
    const viewer = Object.create(ObjectViewer.prototype);
    viewer.object = object;
    viewer.gray = new THREE.MeshStandardMaterial();
    viewer.wireMaterial = new THREE.LineBasicMaterial();
    object.traverse(mesh => { if (mesh.isMesh) mesh.userData.surfaceMaterial = mesh.material; });
    viewer.setDisplay('wire', false);
    const wire = object.getObjectByName('sap1').userData.wire;
    assert(wire.geometry.attributes.position.count > 0);
    viewer.setDisplay('overlay', true);
    assert.equal(object.getObjectByName('sap1').userData.wire, wire, 'wire geometry reused');
    viewer.setDisplay('solid', true);
    assert.equal(wire.visible, false);
    assert.equal(requests, 1, 'display switches do not fetch or render on a server');
    viewer.disposeObject(object);
  } finally { globalThis.fetch = originalFetch; }
});

test('gold body and blue solar panel have distinct realtime materials', () => {
  const material = ObjectViewer.prototype.presetMaterial;
  const gold = material.call({}, {type: 'roughconductor', material: 'Au', alpha: .2});
  const panel = material.call({}, {type: 'diffuse', reflectance: {type: 'rgb', value: [.05, .05, .3]}});
  assert(gold.color.r > gold.color.b);
  assert(panel.color.b > panel.color.r);
  assert(gold.metalness > panel.metalness);
  gold.dispose(); panel.dispose();
});

test('returning to the displayed model invalidates an in-flight replacement', async () => {
  const viewer = Object.create(ObjectViewer.prototype);
  viewer.path = 'A.obj'; viewer.loadingPath = 'B.obj'; viewer.generation = 1;
  viewer.loadedStatus = 'A ready'; viewer.status = {};
  viewer.host = {setAttribute() {}};
  await viewer.load('A.obj');
  assert.equal(viewer.generation, 2);
  assert.equal(viewer.loadingPath, null);
  assert.equal(viewer.status.textContent, 'A ready');
});
