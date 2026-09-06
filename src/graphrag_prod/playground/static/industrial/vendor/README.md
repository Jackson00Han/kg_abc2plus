# Industrial graph browser dependencies

These fixed browser distributions run from the application's own static
directory. They require no CDN, package installation, or network request at
runtime. The two scripts together occupy 481,104 bytes before HTTP compression.

| Package | Version | Delivery | License |
| --- | --- | --- | --- |
| Cytoscape.js | 3.34.2 | `cytoscape.min.js` | MIT |
| cytoscape-dagre | 4.0.1 | `cytoscape-dagre.min.js` | MIT |
| @dagrejs/dagre | 3.0.0 | Included inside the extension | MIT |
| @dagrejs/graphlib | 4.0.1 | Included inside Dagre | MIT |

The extension declares Cytoscape `^3.2.22`; the selected Cytoscape version is
compatible. This extension release bundles Dagre, so loading a separate Dagre
script is unnecessary. See the
[versioned extension instructions](https://github.com/cytoscape/cytoscape.js-dagre/blob/v4.0.1/README.md)
and its [dependency lock](https://github.com/cytoscape/cytoscape.js-dagre/blob/v4.0.1/package-lock.json).
The embedded Dagre source was compared byte for byte with its published 3.0.0
ESM distribution. Its bundled Graphlib version also agrees with the lock.

`sources.v1.json` records exact registry URLs, archive SHA-512 integrity,
archive and retained-file SHA-256 values, byte counts, licenses, and local
transformations. All archives passed their registry SHA-512 checks. Registry
signatures were not independently verified. Original archives and inspection
source maps remain outside the repository.

The published cytoscape-dagre **4.0.1** archive contains a **4.0.0** banner.
That upstream banner remains unchanged; the recorded version comes from the
integrity-verified published package. The extension's unused `sourceMappingURL`
trailer was removed to avoid requesting an omitted map. Final newlines were
normalized where recorded; executable library code was not rebuilt or changed.
The original MIT notices, including the bundled Dagre legal notice, are kept
alongside the scripts.

## Browser loading and API

Load these classic scripts in order, before the application's native ESM
component. Do not add `async` or import a UMD file as if it had an ESM default
export. Adjust the static URL prefix for the host application:

```html
<script src="/static/industrial/vendor/cytoscape.min.js"></script>
<script src="/static/industrial/vendor/cytoscape-dagre.min.js"></script>
<script type="module" src="/static/industrial/graph-component.js"></script>
```

The first script exposes `globalThis.cytoscape`. The second exposes
`globalThis.cytoscapeDagre` and automatically registers the `dagre` layout when
`window.cytoscape` already exists. A module can then use:

```js
const cy = globalThis.cytoscape({ container, elements, style });
cy.layout({
  name: 'dagre', rankDir: 'TB', rankSep: 80, nodeSep: 35,
  nodeDimensionsIncludeLabels: true, animate: false, fit: true
}).run();
```

`rankDir: 'LR'` produces a left-to-right layout. Applications that intentionally
load the extension before Cytoscape must register it afterward with
`cytoscape.use(globalThis.cytoscapeDagre)`. Use one registration path.

Dagre positions supplied nodes and edges; it does not establish an ontology
hierarchy, source authority, diagnosis, or electrical connection. The host
component chooses the appropriate relation subset and keeps original direction
and evidence available. This bundle targets current browsers, not legacy IE.

## Verification

From the repository root, verify every retained upstream file offline:

```sh
.venv/bin/python - <<'PY'
import hashlib, json
from pathlib import Path
base = Path('src/graphrag_prod/playground/static/industrial/vendor')
manifest = json.loads((base / 'sources.v1.json').read_text())
for entry in manifest['files']:
    content = (base / entry['path']).read_bytes()
    assert len(content) == entry['bytes']
    assert hashlib.sha256(content).hexdigest() == entry['sha256']
print('Vendor checksums verified')
PY
```

An offline Node VM check loaded the exact scripts as browser globals without
CommonJS or AMD, confirmed automatic layout registration, and verified finite
ordered positions for a four-level graph in both TB and LR directions without
changing graph identity. This checks bundle integration; rendered UI behavior
is validated separately in the application's browser checks.
