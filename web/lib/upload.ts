/**
 * Client-side mirror of `app/services/source_filter.py`.
 *
 * A project folder routinely contains thousands of irrelevant files (.git
 * objects, node_modules, lock files, parquet blobs). Filtering here means the
 * user sees the real selection *before* uploading, and we never push 500 MB of
 * junk across the wire only for the server to discard it. The lists below must
 * stay aligned with the backend — the backend still filters independently, so a
 * drift here degrades UX but never correctness.
 *
 * Three destinations, mirroring the server:
 *   code   — parsed for lineage
 *   data   — profiled (columns, row count); the rows never leave the server
 *   skip   — irrelevant
 *
 * The one thing a browser cannot cheaply do is read file bodies, so the
 * content-dependent decisions (is this JSON config or a data export? is this
 * SQL minified?) happen server-side only.
 */

const SKIP_DIRS = new Set([
  '.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', 'env',
  '__pycache__', '.tox', '.mypy_cache', '.pytest_cache', '.ruff_cache',
  'dist', 'build', '.idea', '.vscode', 'site-packages', '.next', 'logs',
  '.ipynb_checkpoints',
]);

const CODE_EXT = new Set(['.sql', '.py', '.ddl', '.j2', '.jinja']);
const CONFIG_EXT = new Set(['.yml', '.yaml']);
const DATA_EXT = new Set(['.csv', '.tsv', '.jsonl', '.ndjson', '.parquet']);

const CONFIG_NAMES = new Set([
  'dbt_project.yml', 'profiles.yml', 'packages.yml', 'selectors.yml',
  'dbt_packages.yml',
]);

const JUNK_NAMES = new Set([
  'package-lock.json', 'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml',
  'yarn.lock', 'bun.lock', 'bun.lockb', 'poetry.lock', 'pipfile.lock',
  'uv.lock', 'conda-lock.yml', 'composer.lock', 'gemfile.lock',
  'catalog.json', 'run_results.json', 'semgrep.lock', '.ds_store',
]);

/** Keep dbt's compiled artifacts out; only the manifest from target/ matters. */
const TARGET_KEEP = new Set(['manifest.json']);

export const MAX_FILES = 400;
export const MAX_DATA_BYTES = 8_000_000;
export const MAX_DATA_FILES = 50;
/** A JSON config worth reading is small; bigger means export (server mirrors this). */
export const JSON_CONFIG_MAX_BYTES = 32_000;

export type Kind = 'code' | 'data' | 'skip';

export type NamedFile = {
  file: File;
  /** Path relative to the chosen folder/root; equals `file.name` for loose files. */
  path: string;
};

/**
 * `folder` = a directory pick or drop (strict filtering, junk is expected);
 * `files` = the user named individual files, so they are taken at face value.
 */
export type UploadMode = 'folder' | 'files';

export type FileSelection = {
  files: NamedFile[];
  mode: UploadMode;
  totalSeen: number;
  skipped: number;
  dataFiles: number;
  codeFiles: number;
};

export function relPath(file: File): string {
  // `webkitRelativePath` is only populated by <input webkitdirectory>;
  // drag-and-drop folder walks pass the entry path explicitly instead.
  return file.webkitRelativePath || file.name;
}

function extOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i < 0 ? '' : name.slice(i).toLowerCase();
}

export function isSeedCsv(path: string): boolean {
  const parts = path.toLowerCase().replace(/\\/g, '/').split('/');
  const name = parts[parts.length - 1];
  if (extOf(name) !== '.csv') return false;
  return parts.slice(0, -1).includes('seeds') || name.startsWith('seed_');
}

function underSkipDir(parts: string[]): boolean {
  return parts.slice(0, -1).some((p) => SKIP_DIRS.has(p)) || SKIP_DIRS.has(parts[0]);
}

/** Mirror of the backend `classify()` for the data a browser has (size, path). */
export function classify(nf: NamedFile, mode: UploadMode = 'folder'): Kind {
  const norm = nf.path.replace(/\\/g, '/').replace(/^(\.\/)+/, '');
  const parts = norm.split('/');
  const name = parts[parts.length - 1];
  const low = name.toLowerCase();
  const ext = extOf(low);

  if (underSkipDir(parts)) return 'skip';
  // dbt's target/ may sit below the project dir; only its manifest counts
  const ti = parts.slice(0, -1).indexOf('target');
  if (ti >= 0) {
    const under = parts.slice(ti + 1);
    if (!(under.length === 1 && TARGET_KEEP.has(under[0]))) return 'skip';
  }

  if (mode === 'files') {
    // a deliberate pick is a deliberate pick: only size caps still apply
    return nf.file.size <= MAX_DATA_BYTES ? 'code' : 'skip';
  }

  if (JUNK_NAMES.has(low)) return 'skip';
  if (CODE_EXT.has(ext) || CONFIG_EXT.has(ext) || CONFIG_NAMES.has(low)) return 'code';
  if (DATA_EXT.has(ext)) {
    return nf.file.size <= MAX_DATA_BYTES ? 'data' : 'skip';
  }
  if (ext === '.json') {
    if (TARGET_KEEP.has(low) && ti >= 0) {
      return nf.file.size <= 4_000_000 ? 'code' : 'skip';
    }
    // too big to be config: treat it as a data export. The server still reads
    // the body to be certain, but the count the user sees stays close.
    return nf.file.size <= JSON_CONFIG_MAX_BYTES ? 'code' : 'data';
  }
  if (TARGET_KEEP.has(low)) return nf.file.size <= 4_000_000 ? 'code' : 'skip';
  return 'skip';
}

export function filterUpload(items: NamedFile[], mode: UploadMode = 'folder'): FileSelection {
  const dataFiles: NamedFile[] = [];
  const codeFiles: NamedFile[] = [];
  for (const nf of items) {
    const kind = classify(nf, mode);
    if (kind === 'data') dataFiles.push(nf);
    else if (kind === 'code') codeFiles.push(nf);
  }
  const files = [...codeFiles.slice(0, MAX_FILES), ...dataFiles.slice(0, MAX_DATA_FILES)];
  return {
    files,
    mode,
    totalSeen: items.length,
    skipped: items.length - files.length,
    dataFiles: dataFiles.length,
    codeFiles: Math.min(codeFiles.length, MAX_FILES),
  };
}

export function fromFileList(list: FileList | File[]): NamedFile[] {
  return Array.from(list).map((file) => ({ file, path: relPath(file) }));
}

type FsEntry = {
  name: string;
  fullPath: string;
  isFile: boolean;
  isDirectory: boolean;
};

/**
 * Drag-and-drop of a *folder* yields directory entries, not files — recurse
 * via the (still universal) webkit entry API, and fall back to plain files
 * where entries are unavailable.
 */
export async function fromDataTransfer(dt: DataTransfer): Promise<NamedFile[]> {
  const out: NamedFile[] = [];

  const walk = async (entry: FsEntry): Promise<void> => {
    if (entry.isFile) {
      const file = await new Promise<File>((res, rej) =>
        (entry as unknown as { file: (r: (f: File) => void, j: (e: unknown) => void) => void })
          .file(res, rej),
      );
      out.push({ file, path: entry.fullPath.replace(/^\/+/, '') });
      return;
    }
    if (SKIP_DIRS.has(entry.name)) return; // don't descend into .git / node_modules
    const reader = (entry as unknown as {
      createReader: () => { readEntries: (r: (e: FsEntry[]) => void, j: (e: unknown) => void) => void };
    }).createReader();
    for (;;) {
      // readEntries returns in batches until empty
      const batch = await new Promise<FsEntry[]>((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) break;
      for (const child of batch) await walk(child);
    }
  };

  const items = Array.from(dt.items ?? []);
  const entries = items
    .filter((i) => i.kind === 'file')
    .map((i) =>
      (i as DataTransferItem & { webkitGetAsEntry?: () => FsEntry | null }).webkitGetAsEntry?.() ?? null,
    );

  if (entries.some(Boolean)) {
    for (const e of entries) if (e) await walk(e);
    return out;
  }
  return fromFileList(dt.files); // plain-files drop / browsers without the entry API
}

/** Human summary for the upload card: "34 files · 3 data files to profile of 12,000". */
export function describeSelection(sel: FileSelection): string {
  const { codeFiles, dataFiles, totalSeen } = sel;
  const bits = [`${codeFiles} code/config file${codeFiles === 1 ? '' : 's'}`];
  if (dataFiles > 0) bits.push(`${dataFiles} dataset file${dataFiles === 1 ? '' : 's'} to profile`);
  if (totalSeen === sel.files.length) return bits.join(' + ');
  return `${bits.join(' + ')} of ${totalSeen.toLocaleString()} (${(totalSeen - sel.files.length).toLocaleString()} skipped)`;
}
