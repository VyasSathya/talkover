import React from 'react';
import {
  AbsoluteFill, Audio, Easing, interpolate, OffthreadVideo, Sequence, spring, staticFile, useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import {loadFont} from '@remotion/google-fonts/Inter';

const {fontFamily} = loadFont('normal', {weights: ['400', '500', '600', '700'], subsets: ['latin']});

export const INTRO_S = 2.6;
export const OUTRO_S = 3.4;

type Cam = {t: number; cx: number; cy: number; zoom: number; kind: string};
type Word = {text: string; t: number};
type Caption = {who: 'you' | 'claude'; start: number; end: number; words: Word[]};
type Segment = {t: number; dur: number; speed: number; kind: string};
export type Timeline = {
  fps: number; duration: number; source: {w: number; h: number}; segments: Segment[]; cameras: Cam[];
  captions: Caption[]; cursor: {t: number; x: number; y: number}[]; panel: {w: number; h: number} | null;
  title: string; subtitle: string; url: string; synthetic: boolean; music: boolean;
};

// Palette: neutral dark, teal = you, coral = Claude (the same colors as the panel's orb).
const C = {
  bg: '#0b0c0f', bg2: '#13151a', line: 'rgba(255,255,255,0.08)', text: '#f2f3f5', dim: '#9aa0aa',
  you: '#5eead4', claude: '#fb9a6c',
};

const CARD = {w: 1728, h: 972};
const ease = Easing.bezier(0.33, 0, 0.15, 1);

function camAt(cams: Cam[], t: number) {
  if (!cams.length) return {cx: 0, cy: 0, zoom: 1, kind: ''};
  let i = 0;
  while (i + 1 < cams.length && cams[i + 1].t - 0.3 <= t) i++;
  const cur = cams[i];
  const prev = cams[Math.max(0, i - 1)];
  const p = i === 0 ? 1 : interpolate(t, [cur.t - 0.3, cur.t + 0.6], [0, 1], {
    extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: ease,
  });
  return {
    cx: prev.cx + (cur.cx - prev.cx) * p, cy: prev.cy + (cur.cy - prev.cy) * p,
    zoom: prev.zoom + (cur.zoom - prev.zoom) * p, kind: cur.kind,
  };
}

function cursorAt(cur: Timeline['cursor'], t: number) {
  if (!cur.length) return null;
  let i = 0;
  while (i + 1 < cur.length && cur[i + 1].t <= t) i++;
  const a = cur[i], b = cur[Math.min(i + 1, cur.length - 1)];
  const k = b.t > a.t ? Math.min(1, Math.max(0, (t - a.t) / (b.t - a.t))) : 0;
  return {x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k};
}

const Screen: React.FC<{tl: Timeline; t: number}> = ({tl, t}) => {
  const {w: W, h: H} = tl.source;
  const baseVH = Math.min(H, (W * 9) / 16);
  const baseVW = (baseVH * 16) / 9;
  const cam = camAt(tl.cameras, t);
  const cx = cam.cx || W / 2, cy = cam.cy || H / 2;
  const vw = baseVW / cam.zoom, vh = baseVH / cam.zoom;
  const left = Math.min(Math.max(cx - vw / 2, 0), W - vw);
  const top = Math.min(Math.max(cy - vh / 2, 0), H - vh);
  const s = CARD.h / vh;
  const cur = cursorAt(tl.cursor, t);
  return (
    <div style={{position: 'absolute', inset: 0, overflow: 'hidden'}}>
      <div style={{position: 'absolute', left: 0, top: 0, width: W, height: H, transformOrigin: '0 0',
        transform: `scale(${s}) translate(${-left}px, ${-top}px)`}}>
        <OffthreadVideo src={staticFile('cut.mp4')} style={{width: W, height: H}} />
        {cur ? (
          <div style={{position: 'absolute', left: cur.x - 22, top: cur.y - 22, width: 44, height: 44, borderRadius: 22,
            border: '2px solid rgba(255,255,255,0.28)', boxShadow: '0 0 18px rgba(255,255,255,0.12)'}} />
        ) : null}
      </div>
    </div>
  );
};

const Chip: React.FC<{text: string; color: string; frame: number}> = ({text, color, frame}) => {
  const {fps} = useVideoConfig();
  const inn = spring({frame, fps, config: {damping: 200}, durationInFrames: 10});
  return (
    <div style={{display: 'inline-flex', alignItems: 'center', gap: 10, padding: '10px 18px', borderRadius: 999,
      background: 'rgba(12,13,16,0.82)', border: `1px solid ${C.line}`, color: C.text, fontSize: 24, fontWeight: 500,
      opacity: inn, transform: `translateY(${(1 - inn) * 8}px)`, backdropFilter: 'blur(8px)'}}>
      <span style={{width: 10, height: 10, borderRadius: 5, background: color}} />
      {text}
    </div>
  );
};

const Captions: React.FC<{tl: Timeline; t: number}> = ({tl, t}) => {
  const live = tl.captions.filter((c) => c.start - 0.1 <= t && t <= c.end + 0.7);
  const cap = live[live.length - 1];
  if (!cap) return null;
  const shown = cap.words.filter((w) => w.t <= t + 0.05);
  const last = shown.slice(-11);
  const color = cap.who === 'you' ? C.you : C.claude;
  const fade = interpolate(t, [cap.end + 0.3, cap.end + 0.7], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  return (
    <div style={{position: 'absolute', left: 0, right: 0, bottom: 64, display: 'flex', justifyContent: 'center', opacity: fade}}>
      <div style={{maxWidth: 1400, display: 'flex', alignItems: 'baseline', gap: 18, padding: '18px 28px', borderRadius: 18,
        background: 'rgba(10,11,14,0.86)', border: `1px solid ${C.line}`, backdropFilter: 'blur(10px)'}}>
        <span style={{color, fontSize: 24, fontWeight: 600, letterSpacing: 0.3, textTransform: 'uppercase'}}>
          {cap.who === 'you' ? 'You' : 'Claude'}
        </span>
        <span style={{color: C.text, fontSize: 38, fontWeight: 500, lineHeight: 1.25}}>
          {last.map((w, i) => (
            <span key={i} style={{opacity: i === last.length - 1 ? 1 : 0.86}}>{w.text} </span>
          ))}
        </span>
      </div>
    </div>
  );
};

const Panel: React.FC<{tl: Timeline}> = ({tl}) => {
  if (!tl.panel) return null;
  const pad = 12, k = 1.05;
  const w = (tl.panel.w - pad * 2) * k, h = (tl.panel.h - pad * 2) * k;
  return (
    <div style={{position: 'absolute', right: 36, top: 36, width: w, height: h, overflow: 'hidden', borderRadius: 30 * k,
      boxShadow: '0 18px 50px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.08)'}}>
      <OffthreadVideo src={staticFile('panel.mp4')} muted
        style={{position: 'absolute', left: -pad * k, top: -pad * k, width: tl.panel.w * k, height: tl.panel.h * k}} />
    </div>
  );
};

const Title: React.FC<{title: string; sub: string; frame: number; out?: boolean; url?: string}> = ({title, sub, frame, out, url}) => {
  const {fps} = useVideoConfig();
  const a = spring({frame, fps, config: {damping: 200}, durationInFrames: 18});
  const b = spring({frame: frame - 6, fps, config: {damping: 200}, durationInFrames: 18});
  return (
    <AbsoluteFill style={{alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: 26}}>
      <div style={{fontSize: 96, fontWeight: 700, color: C.text, letterSpacing: -2, opacity: a,
        transform: `translateY(${(1 - a) * 16}px)`}}>{title}</div>
      <div style={{fontSize: 36, fontWeight: 400, color: C.dim, opacity: b, maxWidth: 1300, textAlign: 'center',
        transform: `translateY(${(1 - b) * 12}px)`}}>{sub}</div>
      {out && url ? <div style={{marginTop: 18, fontSize: 32, fontWeight: 500, color: C.you, opacity: b}}>{url}</div> : null}
    </AbsoluteFill>
  );
};

export const Demo: React.FC<{timeline: Timeline}> = ({timeline: tl}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const intro = Math.round(INTRO_S * fps);
  const body = Math.round(tl.duration * fps);
  const t = (frame - intro) / fps;
  const seg = tl.segments.find((s) => s.t <= t && t < s.t + s.dur);
  const segFrame = seg ? Math.round((t - seg.t) * fps) : 0;
  const cardIn = spring({frame: frame - intro + 8, fps, config: {damping: 200}, durationInFrames: 22});
  const cardOut = interpolate(frame, [intro + body - 8, intro + body + 6], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  return (
    <AbsoluteFill style={{background: `radial-gradient(120% 90% at 50% 0%, ${C.bg2} 0%, ${C.bg} 60%)`, fontFamily}}>
      <Sequence durationInFrames={intro + 6}>
        <AbsoluteFill style={{opacity: interpolate(frame, [intro - 8, intro + 4], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})}}>
          <Title title={tl.title} sub={tl.subtitle} frame={frame} />
        </AbsoluteFill>
      </Sequence>

      <Sequence from={intro} durationInFrames={body}>
        <AbsoluteFill style={{opacity: Math.min(cardIn, cardOut)}}>
          <div style={{position: 'absolute', left: (1920 - CARD.w) / 2, top: (1080 - CARD.h) / 2, width: CARD.w, height: CARD.h,
            borderRadius: 20, overflow: 'hidden', border: `1px solid ${C.line}`, boxShadow: '0 40px 120px rgba(0,0,0,0.6)',
            transform: `scale(${0.96 + 0.04 * cardIn})`}}>
            <Screen tl={tl} t={Math.max(0, t)} />
            <Panel tl={tl} />
            <div style={{position: 'absolute', left: 32, top: 32, display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 12}}>
              {tl.synthetic ? (
                <div style={{padding: '8px 14px', borderRadius: 999, background: 'rgba(12,13,16,0.82)', border: `1px solid ${C.line}`,
                  color: C.dim, fontSize: 20, fontWeight: 500}}>Synthetic voice demo (Kokoro)</div>
              ) : null}
              {seg && seg.kind === 'wait' && seg.speed > 1 ? <Chip key={'w' + seg.t} text={`Claude is working  ${seg.speed}x`} color={C.claude} frame={segFrame} /> : null}
              {seg && seg.kind === 'barge' ? <Chip key={'b' + seg.t} text="Talking over it" color={C.you} frame={segFrame} /> : null}
            </div>
          </div>
          <Captions tl={tl} t={t} />
        </AbsoluteFill>
      </Sequence>

      <Sequence from={intro + body}>
        <Title title={tl.title} sub="Free, local, open source." frame={frame - intro - body} out url={tl.url} />
      </Sequence>

      {tl.music ? (
        <Audio src={staticFile('music.mp3')} volume={(f) => {
          const tt = (f - intro) / fps;
          const duck = tl.captions.some((c) => c.start - 0.3 <= tt && tt <= c.end + 0.3);
          const fadeOut = interpolate(f, [intro + body + 30, intro + body + OUTRO_S * fps], [1, 0], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
          return (duck ? 0.05 : 0.16) * fadeOut;
        }} />
      ) : null}
    </AbsoluteFill>
  );
};
