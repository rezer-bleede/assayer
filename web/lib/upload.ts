/**
 * Client-side mirror of `app/services/source_filter.py`.
 *
 * A project folder routinely contains thousands of irrelevant files (.git
 * objects, node_modules, parquet blobs, images). Filtering here means the user
 * sees the real selection *before* uploading, and we never push 500 MB of junk
 * across the wire only for the server to discard it. The lists below must stay
 * aligned with the backend — the backend still filters independently, so a
 * drift here degrades UX but never correctness.
 */

const SKIP_DIRS = new Set([
  '.git', '.hg', '.svn', 'node_modules', '.venv', 'venv', 'env',
  '__pycache__', '.tox', '.mypy_cache', '.pytest_cache', '.ruff_cache',
  'dist', 'build', '.idea', '.vscode', 'site-packages', '.next', 'logs',
  '.ipynb_checkpoints',
]);

const INCLUDE_EXT = new Set([
  '.sql', '.py', '.yml', '.yaml', '.json', '.txt', '.ddl', '.j2', '.jinja',
]);

const INCLUDE_NAMES = new Set([
  'dbt_project.yml', 'profiles.yml', 'packages.yml', 'selectors.yml', 'manifest.json',
]);

export const MAX_FILES = 400;
export const MAX_FILE_BYTES = 256_000;

/** Keep dbt's compiled artifacts out; only the manifest from target/ matters. */
const TARGET_KEEP = new Set(['manifest.json']);

export type NamedFile = {
  file: File;
  /** Path relative to the chosen folder/root; equals `file.name` for loose files. */
  path: string;
};

export type FileSelection = {
  files: NamedFile[];
  totalSeen: number;
  skipped: number;
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
  const norm = path.toLowerCase();
  return extOf(norm) === '.csv' && /(^|\/)seeds?\//.test(norm);
}

function underSkipDir(parts: string[]): boolean {
  return parts.slice(0, -1).some((p) => SKIP_DIRS.has(p)) || SKIP_DIRS.has(parts[0]);
}

/** Mirror of the backend `classify()` for the data a browser has (size, path). */
export function isRelevant(nf: NamedFile): boolean {
  const norm = nf.path.replace(/\\/g, '/').replace(/^(\.\/)+/, '');
  const parts = norm.split('/');
  const name = parts[parts.length - 1];

  if (isSeedCsv(norm)) return true; // name is data; body is skipped server-side
  if (underSkipDir(parts)) return false;
  // dbt's target/ may sit below the project dir; only its manifest counts
  const ti = parts.slice(0, -1).indexOf('target');
  if (ti >= 0) {
    const under = parts.slice(ti + 1);
    if (!(under.length === 1 && TARGET_KEEP.has(under[0]))) return false;
  }
  if (!INCLUDE_EXT.has(extOf(name)) && !INCLUDE_NAMES.has(name)) return false;
  if (ti >= 0 && name === 'manifest.json') return nf.file.size <= 4_000_000;
  return nf.file.size <= MAX_FILE_BYTES;
}

export function filterUpload(items: NamedFile[]): FileSelection {
  const files = items.filter(isRelevant).slice(0, MAX_FILES);
  return {
    files,
    totalSeen: items.length,
    skipped: items.length - files.length,
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

/** Human summary for the upload card, e.g. "34 relevant of 12,000 (11,966 skipped)". */
export function describeSelection(sel: FileSelection): string {
  const kept = sel.files.length;
  if (sel.totalSeen === kept) return `${kept} file${kept === 1 ? '' : 's'} selected`;
  return `${kept} relevant file${kept === 1 ? '' : 's'} of ${sel.totalSeen.toLocaleString()} selected (${(sel.totalSeen - kept).toLocaleString()} skipped)`;
}
