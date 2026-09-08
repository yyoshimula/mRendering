// Resolve the locally vendored browser dependency in CPU-only Node tests.
export function resolve(specifier, context, nextResolve) {
  if (specifier === 'three') return {url: new URL('../gui/vendor/three/build/three.module.min.js', import.meta.url).href, shortCircuit: true};
  return nextResolve(specifier, context);
}
