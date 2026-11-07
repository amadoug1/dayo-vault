const { useState, useEffect, useRef, useCallback } = React;
const { motion, AnimatePresence, useSpring, useMotionValue, useTransform, animate } = window.Motion;
const M = motion;

/* ── helpers ─────────────────────────────────────────────── */
function fmtSize(n) {
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)}${u[i]}`;
}
function fmtAge(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function extOf(name) {
  const m = (name || "").split(".").pop().toUpperCase();
  return m.length <= 4 ? m : "FILE";
}
// Seed suggestions only — you can type ANY category (future classes, personal collections).
// The live filter list is built from whatever categories actually exist in your vault.
const SEED_CATEGORIES = ["EAS 2030", "STSC 1120", "Personal"];
const TX_BADGE = {
  pending:    { label: "queued for transcription", color: "var(--accent)" },
  processing: { label: "transcribing…",            color: "var(--accent-2)" },
  failed:     { label: "transcription failed",     color: "var(--danger)" },
};

/* ── animated counter ────────────────────────────────────── */
function CountUp({ value }) {
  const mv = useMotionValue(0);
  const [disp, setDisp] = useState(0);
  useEffect(() => {
    const controls = animate(mv, value, { duration: 0.9, ease: "easeOut" });
    const unsub = mv.on("change", (v) => setDisp(Math.round(v)));
    return () => { controls.stop(); unsub(); };
  }, [value]);
  return <span>{disp.toLocaleString()}</span>;
}

/* ── stat card ───────────────────────────────────────────── */
function Stat({ num, lbl, isText }) {
  return (
    <M.div className="stat"
      initial={{ opacity: 0, y: 18, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 220, damping: 22 }}>
      <div className="num">{isText ? num : <CountUp value={num} />}</div>
      <div className="lbl">{lbl}</div>
    </M.div>
  );
}

/* ── upload progress row ─────────────────────────────────── */
function UploadRow({ up }) {
  return (
    <M.div className="uprow"
      initial={{ opacity: 0, x: -20 }} animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 20 }} layout>
      <div className="ftype" style={{ width: 36, height: 36, marginBottom: 0 }}>{extOf(up.name)}</div>
      <div className="upname">{up.name}</div>
      <div className="bar"><M.div className="bar-fill"
        animate={{ width: `${up.pct}%` }} transition={{ ease: "easeOut" }} /></div>
      <div className="uppct">{up.pct}%</div>
    </M.div>
  );
}

/* ── file card ───────────────────────────────────────────── */
function FileCard({ f, onOpen, onDelete, onRetry }) {
  const tx = TX_BADGE[f.transcription_status];
  const wordLabel = f.word_count ? `${f.word_count.toLocaleString()} words`
    : (f.transcription_status === "done" ? "no speech" : "no text");
  return (
    <M.div className="card" layout
      initial={{ opacity: 0, y: 26, scale: 0.94 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, scale: 0.9, transition: { duration: 0.18 } }}
      transition={{ type: "spring", stiffness: 260, damping: 24 }}
      whileHover={{ y: -6, borderColor: "rgba(120,200,255,0.45)", boxShadow: "0 18px 50px rgba(0,0,0,0.45), 0 0 30px rgba(77,141,255,0.18)" }}>
      <div className="ftype">{extOf(f.original_name)}</div>
      <div className="fname">{f.original_name}</div>
      <div className="fmeta">
        <span>{wordLabel}</span>
        <span>{fmtSize(f.size)}</span>
        <span>{fmtAge(f.uploaded_at)}</span>
      </div>
      {tx && (
        <M.div className="tx-badge" style={{ color: tx.color, borderColor: tx.color }}
          animate={f.transcription_status === "processing" ? { opacity: [0.5, 1, 0.5] } : { opacity: 1 }}
          transition={{ duration: 1.3, repeat: f.transcription_status === "processing" ? Infinity : 0 }}>
          {f.transcription_status === "processing" && <span className="tx-spin" />}
          {tx.label}
        </M.div>
      )}
      {f.preview && <div className="fpreview">{f.preview}</div>}
      <div className="ftags">
        {f.course && <span className="tag course">{f.course}</span>}
        {(f.tags || []).map((t, i) => <span className="tag" key={i}>{t}</span>)}
      </div>
      <div className="card-actions">
        <button className="btn" onClick={() => onOpen(f)}>VIEW</button>
        <a className="btn" href={`/api/file/${f.id}/download`}>DOWNLOAD</a>
        {f.transcription_status === "failed" && <button className="btn" onClick={() => onRetry(f)}>RETRY</button>}
        <button className="btn danger" onClick={() => onDelete(f)}>DELETE</button>
      </div>
    </M.div>
  );
}

/* ── detail drawer ───────────────────────────────────────── */
function Drawer({ file, onClose }) {
  const [text, setText] = useState("loading…");
  useEffect(() => {
    if (!file) return;
    fetch(`/api/file/${file.id}/text`).then(r => r.text())
      .then(t => setText(t || "(no extracted text for this file type)"))
      .catch(() => setText("(could not load text)"));
  }, [file]);
  if (!file) return null;
  return (
    <>
      <M.div className="drawer-backdrop" onClick={onClose}
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} />
      <M.div className="drawer"
        initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }}
        transition={{ type: "spring", stiffness: 320, damping: 34 }}>
        <button className="drawer-close" onClick={onClose}>×</button>
        <h3>{file.original_name}</h3>
        <div className="meta-line">
          {file.course || "uncategorized"} · {file.word_count?.toLocaleString() || 0} words ·
          {" "}{fmtSize(file.size)} · {file.extractor}
        </div>
        <div className="ftags" style={{ marginBottom: 18 }}>
          {(file.tags || []).map((t, i) => <span className="tag" key={i}>{t}</span>)}
        </div>
        <pre>{text.slice(0, 20000)}</pre>
      </M.div>
    </>
  );
}

/* ── main app ────────────────────────────────────────────── */
function App() {
  const [files, setFiles] = useState([]);
  const [stats, setStats] = useState({ total_files: 0, total_words: 0, total_size: 0 });
  const [uploads, setUploads] = useState({});
  const [drag, setDrag] = useState(false);
  const [query, setQuery] = useState("");
  const [courseFilter, setCourseFilter] = useState("");
  const [openFile, setOpenFile] = useState(null);
  const [toast, setToast] = useState(null);
  const [course, setCourse] = useState("");
  const [tags, setTags] = useState("");
  const inputRef = useRef();

  const refresh = useCallback(() => {
    fetch("/api/files").then(r => r.json()).then(d => setFiles(d.files || []));
    fetch("/api/stats").then(r => r.json()).then(setStats);
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  // While anything is transcribing, poll so cards flip to "done" live.
  const anyActive = files.some(f => f.transcription_status === "pending" || f.transcription_status === "processing");
  useEffect(() => {
    if (!anyActive) return;
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [anyActive, refresh]);

  const flash = (msg) => { setToast(msg); setTimeout(() => setToast(null), 2600); };

  const retry = (f) => {
    fetch(`/api/file/${f.id}/transcribe`, { method: "POST" })
      .then(() => { flash(`re-queued ${f.original_name} for transcription`); refresh(); });
  };

  const uploadOne = useCallback((file) => {
    const id = Math.random().toString(36).slice(2, 9);
    setUploads(u => ({ ...u, [id]: { name: file.name, pct: 0 } }));
    const fd = new FormData();
    fd.append("file", file);
    fd.append("course", course);
    fd.append("tags", tags);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        setUploads(u => u[id] ? { ...u, [id]: { ...u[id], pct } } : u);
      }
    };
    xhr.onload = () => {
      setUploads(u => { const n = { ...u }; delete n[id]; return n; });
      if (xhr.status === 200) { flash(`✓ ${file.name} added to inventory`); refresh(); }
      else flash(`✗ ${file.name} failed (${xhr.status})`);
    };
    xhr.onerror = () => {
      setUploads(u => { const n = { ...u }; delete n[id]; return n; });
      flash(`✗ ${file.name} — network error`);
    };
    xhr.send(fd);
  }, [course, tags, refresh]);

  const handleFiles = (fileList) => { Array.from(fileList).forEach(uploadOne); };

  const onDrop = (e) => {
    e.preventDefault(); setDrag(false);
    if (e.dataTransfer.files?.length) handleFiles(e.dataTransfer.files);
  };

  const del = (f) => {
    fetch(`/api/file/${f.id}/delete`, { method: "POST" })
      .then(() => { flash(`moved ${f.original_name} to trash`); refresh(); });
  };

  const shown = files.filter(f => {
    if (courseFilter && (f.course || "") !== courseFilter) return false;
    if (query) {
      const q = query.toLowerCase();
      return (f.original_name + " " + (f.preview || "") + " " + (f.tags || []).join(" ") + " " + (f.course || "")).toLowerCase().includes(q);
    }
    return true;
  });
  const upList = Object.entries(uploads).map(([id, u]) => ({ id, ...u }));

  // Categories shown as filter chips = everything that actually exists in the vault,
  // plus the seed suggestions — so future classes / personal collections appear automatically.
  const existingCats = [...new Set(files.map(f => f.course).filter(Boolean))];
  const allCats = [...new Set([...existingCats, ...SEED_CATEGORIES])];

  return (
    <div className="wrap">
      <div className="aurora"><div className="aurora-blob" /></div>
      <div className="grid-overlay" />
      <div className="scanline" />

      {/* top bar */}
      <M.div className="topbar"
        initial={{ opacity: 0, y: -16 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
        <div className="brand">
          <M.div className="brand-mark"
            animate={{
              rotate: [0, -1.5, 1.5, 0],
              boxShadow: [
                "0 0 42px rgba(66,216,255,0.42), inset 0 0 18px rgba(255,255,255,0.28)",
                "0 0 56px rgba(109,255,191,0.50), inset 0 0 22px rgba(255,255,255,0.32)",
                "0 0 42px rgba(66,216,255,0.42), inset 0 0 18px rgba(255,255,255,0.28)",
              ],
            }}
            transition={{ duration: 4.6, repeat: Infinity, ease: "easeInOut" }}>
            <span>DV</span>
          </M.div>
          <div>
            <h1>Dayo Vault</h1>
            <div className="sub">Source Inventory · No Limits</div>
          </div>
        </div>
        <div className="status-pill"><span className="dot" /> LOCAL · :8500 · UNLIMITED UPLOAD</div>
      </M.div>

      {/* stats */}
      <div className="stats">
        <Stat num={stats.total_files} lbl="Sources" />
        <Stat num={stats.total_words} lbl="Words Indexed" />
        <Stat num={fmtSize(stats.total_size)} lbl="Stored" isText />
        <Stat num={Object.keys(stats.by_course || {}).length} lbl="Courses" />
      </div>

      {/* dropzone */}
      <M.div className={"dropzone" + (drag ? " drag" : "")}
        onClick={() => inputRef.current.click()}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={onDrop}
        animate={drag
          ? { scale: 1.012, borderColor: "rgba(33,230,193,0.9)", backgroundColor: "rgba(33,230,193,0.05)" }
          : { scale: 1, borderColor: "rgba(120,200,255,0.45)" }}
        transition={{ type: "spring", stiffness: 300, damping: 26 }}>
        <M.div className="sheen"
          animate={{ left: ["-60%", "160%"] }}
          transition={{ duration: 3.2, repeat: Infinity, ease: "easeInOut", repeatDelay: 1.4 }} />
        <M.div className="ring"
          animate={{ scale: drag ? [1, 1.12, 1] : 1 }}
          transition={{ duration: 0.8, repeat: drag ? Infinity : 0 }}>
          <span>{drag ? "DROP" : "SYNC"}</span>
        </M.div>
        <h2>{drag ? "Drop to add to the vault" : "Drag files here, or click to browse"}</h2>
        <p>PDFs, Word docs, slides, datasets, anything — text is auto-extracted for Dayo</p>
        <div className="hint">NO 50MB LIMIT · STREAMED IN CHUNKS</div>
        <input ref={inputRef} type="file" multiple hidden
          onChange={(e) => handleFiles(e.target.files)} />
      </M.div>

      {/* upload meta — category is free-text: any class or personal collection */}
      <div className="upload-meta">
        <input list="cat-options" placeholder="Category — class or anything (e.g. CIS 1200, Recipes)"
          value={course} onChange={(e) => setCourse(e.target.value)} />
        <datalist id="cat-options">
          {allCats.map(c => <option key={c} value={c} />)}
        </datalist>
        <input placeholder="tags, comma, separated" value={tags} onChange={(e) => setTags(e.target.value)} />
        <div className="meta-hint">
          category + tags apply to the next files you add
        </div>
      </div>

      {/* uploads in flight */}
      <AnimatePresence>
        {upList.length > 0 && (
          <M.div style={{ marginTop: 22 }} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <div className="section-title">Uploading</div>
            <AnimatePresence>{upList.map(u => <UploadRow key={u.id} up={u} />)}</AnimatePresence>
          </M.div>
        )}
      </AnimatePresence>

      {/* toolbar */}
      <div className="toolbar">
        <input className="search" placeholder="Search sources, tags, content…"
          value={query} onChange={(e) => setQuery(e.target.value)} />
        <div className="chips">
          <div className={"chip" + (courseFilter === "" ? " active" : "")} onClick={() => setCourseFilter("")}>ALL</div>
          {allCats.map(c => (
            <div key={c} className={"chip" + (courseFilter === c ? " active" : "")} onClick={() => setCourseFilter(c)}>{c}</div>
          ))}
        </div>
      </div>

      {/* grid */}
      <div className="section-title">{shown.length} Source{shown.length !== 1 ? "s" : ""}</div>
      {shown.length === 0 ? (
        <M.div className="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          <div className="big">EMPTY INDEX</div>
          <div>{files.length === 0 ? "Your vault is empty. Drop your first file above." : "Nothing matches that filter."}</div>
        </M.div>
      ) : (
        <M.div className="grid" layout>
          <AnimatePresence>
            {shown.map(f => <FileCard key={f.id} f={f} onOpen={setOpenFile} onDelete={del} onRetry={retry} />)}
          </AnimatePresence>
        </M.div>
      )}

      {/* drawer */}
      <AnimatePresence>{openFile && <Drawer file={openFile} onClose={() => setOpenFile(null)} />}</AnimatePresence>

      {/* toast */}
      <AnimatePresence>
        {toast && (
          <M.div className="toast"
            initial={{ opacity: 0, y: 30, x: "-50%" }} animate={{ opacity: 1, y: 0, x: "-50%" }}
            exit={{ opacity: 0, y: 20, x: "-50%" }}>{toast}</M.div>
        )}
      </AnimatePresence>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
