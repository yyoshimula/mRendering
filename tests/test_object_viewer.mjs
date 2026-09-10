// node --loader ./tests/three_loader.mjs --test tests/test_object_viewer.mjs
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {existsSync} from 'node:fs';

// 配布版には無いモデル（internal/、gitignore 対象）を使うテストは無ければスキップ
const AKATSUKI = new URL('../internal/models/akatsuki.obj', import.meta.url);
import * as THREE from 'three';
import {ObjectViewer} from '../gui/object_viewer.js';

test('mixed OBJ faces and loose lines retain Akatsuki surface meshes', {skip: !existsSync(AKATSUKI)}, async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async () => { requests++; return new Response(await readFile(AKATSUKI)); };
  try {
    const object = await ObjectViewer.prototype.readModel.call({}, {url: '/model-assets/internal/models/akatsuki.obj', parts: {PLANET_C: {}}});
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

test('off-centre model keeps its file origin at the axes and fits in view', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({parts: {}, default_material: {}}));
  const viewer = Object.create(ObjectViewer.prototype);
  Object.assign(viewer, {
    generation: 0, status: {}, host: {setAttribute() {}}, scene: new THREE.Scene(),
    camera: new THREE.PerspectiveCamera(40, 1.5, .001, 100), resize() {},
    controls: {target: new THREE.Vector3(), reset() {}, update() { viewer.camera.lookAt(this.target); }},
    applyDisplay() {},
    async readModel() {
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(2, 2, 2), new THREE.MeshBasicMaterial());
      mesh.geometry.translate(8, 0, 3);
      return new THREE.Group().add(mesh);
    },
  });
  try {
    await viewer.load('offset.obj');
    assert.equal(viewer.path, 'offset.obj', viewer.status.textContent);
    const origin = viewer.object.localToWorld(new THREE.Vector3());
    assert(origin.length() < 1e-12, 'model origin must coincide with world axes');
    const bounds = new THREE.Box3().setFromObject(viewer.object);
    assert(bounds.min.x > 0, 'off-centre geometry must not be recentered');
    viewer.camera.updateMatrixWorld(true);
    for (const x of [bounds.min.x, bounds.max.x]) for (const y of [bounds.min.y, bounds.max.y]) for (const z of [bounds.min.z, bounds.max.z]) {
      const projected = new THREE.Vector3(x, y, z).project(viewer.camera);
      assert(Math.abs(projected.x) < 1 && Math.abs(projected.y) < 1 && Math.abs(projected.z) < 1);
    }
    assert(new THREE.Vector3().project(viewer.camera).length() < Math.sqrt(3));
    viewer.disposeObject(viewer.object);
  } finally { globalThis.fetch = originalFetch; }
});
