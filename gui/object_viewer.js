import * as THREE from 'three';
import { OrbitControls } from './vendor/three/examples/jsm/controls/OrbitControls.js';
import { OBJLoader } from './vendor/three/examples/jsm/loaders/OBJLoader.js';
import { MTLLoader } from './vendor/three/examples/jsm/loaders/MTLLoader.js';
import { GLTFLoader } from './vendor/three/examples/jsm/loaders/GLTFLoader.js';
import { DRACOLoader } from './vendor/three/examples/jsm/loaders/DRACOLoader.js';
import { PLYLoader } from './vendor/three/examples/jsm/loaders/PLYLoader.js';

// All pointer interaction stays in this module: no worker, network or Mitsuba calls.
export class ObjectViewer {
  constructor(host, status) {
    this.host = host;
    this.status = status;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x090d14);
    this.camera = new THREE.PerspectiveCamera(40, 1, .001, 100);
    this.renderer = new THREE.WebGLRenderer({antialias: true});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.domElement.setAttribute('aria-label', 'モデルの3D表示。ドラッグで回転、ホイールで拡大縮小');
    this.renderer.domElement.tabIndex = 0;
    host.appendChild(this.renderer.domElement);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = .12;
    this.controls.minDistance = .15;
    this.controls.maxDistance = 50;
    this.controls.addEventListener('change', () => { this.dirty = true; });
    this.scene.add(new THREE.HemisphereLight(0xe1edff, 0x6b5d45, 2.5));
    for (const [position, intensity] of [[[3, 4, 5], 3], [[-4, 1, -2], 2]]) {
      const light = new THREE.DirectionalLight(0xffffff, intensity);
      light.position.set(...position);
      this.scene.add(light);
    }
    // A tiny studio environment makes metallic gold visible without any background image.
    const envScene = new THREE.Scene();
    envScene.background = new THREE.Color(0x909aaa);
    for (const position of [[0, 3, 0], [4, 0, 2], [-3, 1, -2]]) {
      const panel = new THREE.Mesh(new THREE.PlaneGeometry(4, 4), new THREE.MeshBasicMaterial({color: 0xffffff, side: THREE.DoubleSide}));
      panel.position.set(...position); panel.lookAt(0, 0, 0); envScene.add(panel);
    }
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    this.environment = pmrem.fromScene(envScene, .08);
    this.scene.environment = this.environment.texture;
    envScene.traverse(o => { o.geometry?.dispose(); o.material?.dispose(); });
    pmrem.dispose();
    [0xf36b65, 0x70dc7b, 0x6a9eff].forEach((color, i) => {
      const direction = new THREE.Vector3(); direction.setComponent(i, 1);
      this.scene.add(new THREE.ArrowHelper(direction, new THREE.Vector3(), 1.3, color, .14, .06));
    });
    this.gray = new THREE.MeshStandardMaterial({color: 0xaaaaaa, roughness: .75, side: THREE.DoubleSide});
    this.wireMaterial = new THREE.LineBasicMaterial({color: 0x51c8ed});
    this.draco = new DRACOLoader();
    this.draco.setDecoderPath('/gui/vendor/three/examples/jsm/libs/draco/gltf/');
    this.gltfLoader = new GLTFLoader().setDRACOLoader(this.draco);
    this.mode = 'solid'; this.materialsEnabled = true;
    this.generation = 0; this.path = null; this.dirty = true; this.renderCount = 0;
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(host);
    this.renderer.domElement.addEventListener('webglcontextlost', e => {
      e.preventDefault(); status.textContent = '3D表示が中断されました。画面を再読み込みしてください。';
    });
    this.renderer.domElement.addEventListener('dblclick', () => this.fit());
    this.fit();
  }
  resize() {
    const w = this.host.clientWidth, h = this.host.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix(); this.dirty = true;
  }
  setActive(active) {
    if (this.active === active) return;
    this.active = active;
    if (!active) { cancelAnimationFrame(this.raf); return; }
    this.resize();
    const tick = () => {
      if (!this.active) return;
      this.controls.update();
      if (this.dirty) {
        this.renderer.render(this.scene, this.camera);
        this.renderer.domElement.dataset.renderCount = String(++this.renderCount);
        this.dirty = false;
      }
      this.raf = requestAnimationFrame(tick);
    };
    tick();
  }
  fit() {
    this.resize();
    const angle = Math.atan(Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2)) * Math.min(1, this.camera.aspect));
    this.controls.reset();
    // Frame the mesh and its origin axes without translating the model itself.
    const bounds = this.object ? new THREE.Box3().setFromObject(this.object) : new THREE.Box3();
    bounds.expandByPoint(new THREE.Vector3());
    for (let i = 0; i < 3; i++) {
      const tip = new THREE.Vector3(); tip.setComponent(i, 1.36);
      bounds.expandByPoint(tip);
    }
    const center = bounds.getCenter(new THREE.Vector3());
    const radius = bounds.getSize(new THREE.Vector3()).length() / 2;
    const distance = 1.1 * radius / Math.sin(angle);
    this.camera.position.copy(new THREE.Vector3(3, 2, 3).normalize().multiplyScalar(distance).add(center));
    this.camera.far = Math.max(100, distance + radius * 4);
    this.camera.updateProjectionMatrix();
    this.controls.maxDistance = Math.max(50, distance * 4);
    this.controls.target.copy(center); this.controls.update(); this.dirty = true;
  }
  async readModel(info) {
    const url = info.url, extension = url.split('.').pop().toLowerCase();
    if (extension === 'glb' || extension === 'gltf') return (await this.gltfLoader.loadAsync(url)).scene;
    if (extension === 'ply') return new THREE.Group().add(new THREE.Mesh(await new PLYLoader().loadAsync(url), this.gray.clone()));
    const response = await fetch(url);
    if (!response.ok) throw new Error('モデルファイルを読み込めません');
    const text = await response.text();
    const loader = new OBJLoader();
    const mtl = /^mtllib\s+(.+)$/m.exec(text);
    if (mtl && !Object.keys(info.parts).length) {
      // Restrict embedded texture/material references to this model server.
      const manager = new THREE.LoadingManager();
      manager.setURLModifier(url => {
        const resolved = new URL(url, location.href);
        if (resolved.origin !== location.origin || !resolved.pathname.startsWith('/model-assets/')) throw new Error('外部の材質参照は読み込めません');
        return resolved.href;
      });
      try {
        const materials = await new MTLLoader(manager).loadAsync(new URL(mtl[1].trim(), new URL(url, location.href)).href);
        materials.preload(); loader.setMaterials(materials);
      } catch (_) { /* Missing MTL: keep the neutral surface; model geometry is still useful. */ }
    }
    // Loose-edge records in the satellite OBJs otherwise turn mixed face/line
    // objects into LineSegments in OBJLoader, dropping their surface materials.
    return loader.parse(text.replace(/^[lp]\s.*$/gm, ''));
  }
  presetMaterial(bsdf) {
    if (bsdf.type === 'twosided' && typeof bsdf.material === 'object') return this.presetMaterial(bsdf.material);
    if (bsdf.type === 'blendbsdf') {
      const diffuse = bsdf.bsdf_0 || {}, specular = bsdf.bsdf_1 || {};
      return this.presetMaterial({...specular, base_color: diffuse.reflectance || specular.specular_reflectance,
                                  metallic: .5, roughness: specular.alpha || .4});
    }
    // Shape inspection approximates the physical BSDF with a realtime PBR material.
    const colorDef = bsdf.base_color || bsdf.reflectance || bsdf.diffuse_reflectance;
    const values = Array.isArray(colorDef?.value) ? colorDef.value : [.65, .65, .65];
    const gold = bsdf.material === 'Au';
    const material = new THREE.MeshStandardMaterial({
      color: gold ? 0xe6b84f : new THREE.Color(...values),
      metalness: bsdf.metallic ?? (bsdf.type?.includes('conductor') ? .8 : .05),
      roughness: bsdf.roughness ?? Math.max(.15, bsdf.alpha ?? .5), side: THREE.DoubleSide,
    });
    if (colorDef?.type === 'bitmap' && colorDef.filename?.startsWith('models/')) {
      const url = '/model-assets/' + colorDef.filename.slice(7).split('/').map(encodeURIComponent).join('/');
      material.map = new THREE.TextureLoader().load(url, () => { this.dirty = true; });
      material.map.colorSpace = THREE.SRGBColorSpace;
    }
    return material;
  }
  async load(path) {
    path = path || '';
    if (this.path === path) {
      if (this.loadingPath != null) {
        ++this.generation; this.loadingPath = null;
        this.status.textContent = this.loadedStatus;
        this.host.setAttribute('aria-busy', 'false');
      }
      return;
    }
    if (this.loadingPath === path) return;
    this.loadingPath = path;
    const generation = ++this.generation;
    this.status.textContent = 'モデルを読み込み中…';
    this.host.setAttribute('aria-busy', 'true');
    let object;
    try {
      const response = await fetch('/api/model-info?path=' + encodeURIComponent(path));
      const info = await response.json();
      if (!response.ok) throw new Error(info.error);
      object = await this.readModel(info);
      if (generation !== this.generation) { this.disposeObject(object); return; }
      object.updateMatrixWorld(true);
      const box = new THREE.Box3().setFromObject(object);
      const radius = box.getSize(new THREE.Vector3()).length() / 2;
      if (!Number.isFinite(radius) || radius <= 0) throw new Error('頂点が潰れたモデルです。元のGLBを選択してください。');
      const group = new THREE.Group(); group.add(object); group.scale.setScalar(1 / radius); // Keep the OBJ origin coincident with the axis origin.
      let triangles = 0;
      object.traverse(mesh => {
        if (!mesh.isMesh) return;
        const preset = info.parts[mesh.name] || (Object.keys(info.default_material).length ? info.default_material : null);
        mesh.userData.embeddedMaterial = mesh.material;
        mesh.userData.surfaceMaterial = preset ? this.presetMaterial(preset) : mesh.material;
        for (const mat of [mesh.userData.surfaceMaterial].flat()) { mat.side = THREE.DoubleSide; mat.polygonOffset = true; mat.polygonOffsetFactor = 1; mat.polygonOffsetUnits = 1; }
        triangles += (mesh.geometry.index?.count || mesh.geometry.attributes.position.count) / 3;
      });
      if (this.object) { this.scene.remove(this.object); this.disposeObject(this.object); }
      this.object = group; this.scene.add(group); this.path = path;
      this.applyDisplay(); this.fit();
      this.loadedStatus = `${Math.round(triangles).toLocaleString()} 三角形 · ブラウザ内で表示`;
      this.status.textContent = this.loadedStatus;
    } catch (error) {
      if (generation === this.generation) {
        if (object && object.parent !== this.object) this.disposeObject(object);
        this.status.textContent = 'モデル表示エラー: ' + error.message;
      }
    } finally { if (generation === this.generation) { this.loadingPath = null; this.host.setAttribute('aria-busy', 'false'); } }
  }
  setDisplay(mode, materialsEnabled) {
    this.mode = mode; this.materialsEnabled = materialsEnabled;
    this.applyDisplay();
  }
  applyDisplay() {
    if (!this.object) return;
    this.object.traverse(mesh => {
      if (!mesh.isMesh) return;
      if (this.mode !== 'solid' && !mesh.userData.wire) {
        // WireframeGeometry retains every triangle edge, including triangulation diagonals.
        const wire = new THREE.LineSegments(new THREE.WireframeGeometry(mesh.geometry), this.wireMaterial);
        mesh.add(wire); mesh.userData.wire = wire;
      }
      mesh.material = this.materialsEnabled ? mesh.userData.surfaceMaterial : this.gray;
      // Keep parent meshes visible so their wire children can show through in wire-only mode.
      mesh.layers.set(this.mode === 'wire' ? 1 : 0);
      if (mesh.userData.wire) mesh.userData.wire.visible = this.mode !== 'solid';
    });
    this.dirty = true;
  }
  disposeObject(object) {
    const geometries = new Set(), materials = new Set(), textures = new Set();
    object.traverse(o => {
      if (o.geometry) geometries.add(o.geometry);
      for (const m of [o.material, o.userData.surfaceMaterial, o.userData.embeddedMaterial].flat()) if (m) materials.add(m);
    });
    geometries.forEach(g => g.dispose());
    materials.forEach(m => {
      if (m === this.gray || m === this.wireMaterial) return;
      for (const value of Object.values(m)) if (value?.isTexture) textures.add(value);
      m.dispose();
    });
    textures.forEach(t => t.dispose());
  }
}
