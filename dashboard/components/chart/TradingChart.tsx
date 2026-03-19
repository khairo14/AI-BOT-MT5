"use client";
import { useEffect, useRef } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  Time,
  ColorType,
  CrosshairMode,
  LineStyle,
} from "lightweight-charts";
import type { OHLCVBar, Position } from "@/types";

type LineKey    = "ema8" | "ema20" | "ema21" | "ema50" | "ema200" | "sma20" | "vwap";
type OverlayKey = LineKey | "bbands" | "volume";
type SubKey     = "rsi" | "macd" | "stoch" | "atr" | "adx";
type AllKey     = OverlayKey | SubKey;

export type { AllKey };
export interface CustomMA {
  id: string;
  period: number;
  type: "ema" | "sma";
  color: string;
}
export const IND_DEFAULTS: Record<AllKey, boolean> = {
  ema8:false, ema20:true, ema21:false, ema50:true, ema200:false,
  sma20:false, bbands:false, vwap:false, volume:true,
  rsi:false, macd:false, stoch:false, atr:false, adx:false,
};
export { LABEL as IND_LABEL, COLOR as IND_COLOR, GROUPS as IND_GROUPS };

interface Props {
  bars: OHLCVBar[];
  height?: number;
  positions?: Position[];
  ind: Record<AllKey, boolean>;
  onToggle: (k: AllKey) => void;
  customMAs?: CustomMA[];
}

const LABEL: Record<AllKey, string> = {
  ema8:"EMA 8", ema20:"EMA 20", ema21:"EMA 21", ema50:"EMA 50", ema200:"EMA 200",
  sma20:"SMA 20", bbands:"Bollinger Bands", vwap:"VWAP", volume:"Volume",
  rsi:"RSI (14)", macd:"MACD (12,26,9)", stoch:"Stochastic (5,3,3)", atr:"ATR (14)", adx:"ADX (14)",
};
const COLOR: Record<AllKey, string> = {
  ema8:"#06b6d4", ema20:"#f59e0b", ema21:"#f97316", ema50:"#3b82f6", ema200:"#a855f7",
  sma20:"#94a3b8", bbands:"#475569", vwap:"#2dd4bf", volume:"#334155",
  rsi:"#22d3ee", macd:"#818cf8", stoch:"#f43f5e", atr:"#fb923c", adx:"#a3e635",
};
const GROUPS: { id: string; label: string; keys: AllKey[] }[] = [
  { id:"trend",       label:"Trend",       keys:["ema8","ema20","ema21","ema50","ema200","sma20","bbands","vwap"] },
  { id:"volume",      label:"Volume",      keys:["volume"] },
  { id:"oscillators", label:"Oscillators", keys:["rsi","macd","stoch"] },
  { id:"volatility",  label:"Volatility",  keys:["atr","adx"] },
];
const SUB_KEYS: SubKey[] = ["rsi","macd","stoch","atr","adx"];
const SUB_LABEL: Record<SubKey, string> = {
  rsi:"RSI (14)", macd:"MACD (12,26,9)", stoch:"Stochastic (5,3,3)", atr:"ATR (14)", adx:"ADX / DI (14)",
};
const SUB_H: Record<SubKey, number> = { rsi:100, macd:110, stoch:100, atr:80, adx:100 };

const CHART_OPTS = {
  layout: { background: { type: ColorType.Solid, color: "#0f172a" }, textColor: "#94a3b8", attributionLogo: false },
  grid:       { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
  crosshair:  { mode: CrosshairMode.Normal },
  rightPriceScale: { borderColor: "#1e293b", minimumWidth: 52 },
  timeScale:  { borderColor: "#1e293b", timeVisible: true, secondsVisible: false },
} as const;

function _ema(arr: number[], p: number): (number | null)[] {
  const k = 2 / (p + 1);
  const out: (number | null)[] = Array(arr.length).fill(null);
  if (arr.length < p) return out;
  let e = arr.slice(0, p).reduce((a, b) => a + b, 0) / p;
  out[p - 1] = e;
  for (let i = p; i < arr.length; i++) { e = arr[i] * k + e * (1 - k); out[i] = e; }
  return out;
}
function _sma(arr: number[], p: number): (number | null)[] {
  const out: (number | null)[] = Array(arr.length).fill(null);
  for (let i = p - 1; i < arr.length; i++)
    out[i] = arr.slice(i - p + 1, i + 1).reduce((a, b) => a + b, 0) / p;
  return out;
}
function _bb(closes: number[], p = 20, mult = 2) {
  const mid = _sma(closes, p);
  const upper: (number | null)[] = Array(closes.length).fill(null);
  const lower: (number | null)[] = Array(closes.length).fill(null);
  for (let i = p - 1; i < closes.length; i++) {
    const m = mid[i] as number;
    const variance = closes.slice(i - p + 1, i + 1).reduce((s, x) => s + (x - m) ** 2, 0) / p;
    const sd = Math.sqrt(variance) * mult;
    upper[i] = m + sd; lower[i] = m - sd;
  }
  return { mid, upper, lower };
}
function _rsi(c: number[], p = 14): (number | null)[] {
  const out: (number | null)[] = Array(c.length).fill(null);
  if (c.length < p + 1) return out;
  let ag = 0, al = 0;
  for (let i = 1; i <= p; i++) { const d = c[i] - c[i - 1]; ag += Math.max(d, 0); al += Math.max(-d, 0); }
  ag /= p; al /= p;
  out[p] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  for (let i = p + 1; i < c.length; i++) {
    const d = c[i] - c[i - 1];
    ag = (ag * (p - 1) + Math.max(d, 0)) / p;
    al = (al * (p - 1) + Math.max(-d, 0)) / p;
    out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  }
  return out;
}
function _macd(c: number[]) {
  const e12 = _ema(c, 12), e26 = _ema(c, 26);
  const m = e12.map((v, i) => v !== null && e26[i] !== null ? v - e26[i]! : null);
  const sig = _ema(m.map(v => v ?? 0), 9).map((v, i) => m[i] !== null ? v : null);
  const hist = m.map((v, i) => v !== null && sig[i] !== null ? v - sig[i]! : null);
  return { macd: m, signal: sig, histogram: hist };
}
function _stoch(h: number[], l: number[], c: number[], kp = 5, dp = 3) {
  const rawK: (number | null)[] = Array(c.length).fill(null);
  for (let i = kp - 1; i < c.length; i++) {
    const hh = Math.max(...h.slice(i - kp + 1, i + 1));
    const ll = Math.min(...l.slice(i - kp + 1, i + 1));
    rawK[i] = hh === ll ? 50 : ((c[i] - ll) / (hh - ll)) * 100;
  }
  const ks = _sma(rawK.map(v => v ?? 0), dp).map((v, i) => rawK[i] !== null ? v : null);
  const ds = _sma(ks.map(v => v ?? 0), dp).map((v, i) => ks[i] !== null ? v : null);
  return { k: ks, d: ds };
}
function _atr(h: number[], l: number[], c: number[], p = 14): (number | null)[] {
  const trs = [h[0] - l[0]];
  for (let i = 1; i < c.length; i++)
    trs.push(Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1])));
  const out: (number | null)[] = Array(c.length).fill(null);
  if (trs.length < p) return out;
  let a = trs.slice(0, p).reduce((s, v) => s + v, 0) / p;
  out[p - 1] = a;
  for (let i = p; i < trs.length; i++) { a = (a * (p - 1) + trs[i]) / p; out[i] = a; }
  return out;
}
function _adx(h: number[], l: number[], c: number[], p = 14) {
  const n = c.length;
  const none = () => Array(n).fill(null) as (number | null)[];
  if (n < p * 2) return { adx: none(), pdi: none(), mdi: none() };
  const pdm: number[] = [], mdm: number[] = [], trs: number[] = [];
  for (let i = 1; i < n; i++) {
    const up = h[i] - h[i - 1], dn = l[i - 1] - l[i];
    pdm.push(up > dn && up > 0 ? up : 0);
    mdm.push(dn > up && dn > 0 ? dn : 0);
    trs.push(Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1])));
  }
  const wilder = (arr: number[]) => {
    const o: number[] = Array(arr.length).fill(0);
    if (arr.length < p) return o;
    o[p - 1] = arr.slice(0, p).reduce((a, b) => a + b, 0);
    for (let i = p; i < arr.length; i++) o[i] = o[i - 1] - o[i - 1] / p + arr[i];
    return o;
  };
  const sTR = wilder(trs), sPDM = wilder(pdm), sMDM = wilder(mdm);
  const pdi_ = sTR.map((t, i) => t === 0 ? 0 : (sPDM[i] / t) * 100);
  const mdi_ = sTR.map((t, i) => t === 0 ? 0 : (sMDM[i] / t) * 100);
  const dx_  = pdi_.map((p2, i) => { const s = p2 + mdi_[i]; return s === 0 ? 0 : (Math.abs(p2 - mdi_[i]) / s) * 100; });
  const adxE = _ema(dx_, p);
  const adxA: (number | null)[] = none(), pdiA: (number | null)[] = none(), mdiA: (number | null)[] = none();
  for (let i = p - 1; i < n - 1; i++) { adxA[i + 1] = adxE[i]; pdiA[i + 1] = pdi_[i]; mdiA[i + 1] = mdi_[i]; }
  return { adx: adxA, pdi: pdiA, mdi: mdiA };
}
function _vwap(bars: OHLCVBar[]): (number | null)[] {
  const out: (number | null)[] = Array(bars.length).fill(null);
  let cpv = 0, cv = 0, prevD = "";
  for (let i = 0; i < bars.length; i++) {
    const d = bars[i].time.slice(0, 10);
    if (d !== prevD) { cpv = 0; cv = 0; prevD = d; }
    const tp = (bars[i].high + bars[i].low + bars[i].close) / 3;
    cpv += tp * bars[i].volume; cv += bars[i].volume;
    out[i] = cv > 0 ? cpv / cv : null;
  }
  return out;
}
const mapV = (vals: (number | null)[], times: Time[]) =>
  vals.map((v, i) => v !== null ? { time: times[i], value: v } : null)
      .filter(Boolean) as { time: Time; value: number }[];

export default function TradingChart({ bars, height = 420, positions, ind, onToggle, customMAs = [] }: Props) {
  const mainRef  = useRef<HTMLDivElement>(null);
  const rsiRef   = useRef<HTMLDivElement>(null);
  const macdRef  = useRef<HTMLDivElement>(null);
  const stochRef = useRef<HTMLDivElement>(null);
  const atrRef   = useRef<HTMLDivElement>(null);
  const adxRef   = useRef<HTMLDivElement>(null);

  const mainChart  = useRef<IChartApi | null>(null);
  const rsiChart   = useRef<IChartApi | null>(null);
  const macdChart  = useRef<IChartApi | null>(null);
  const stochChart = useRef<IChartApi | null>(null);
  const atrChart   = useRef<IChartApi | null>(null);
  const adxChart   = useRef<IChartApi | null>(null);

  const candleS = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lineS   = useRef<Partial<Record<LineKey, ISeriesApi<"Line"> | null>>>({});
  const bbS     = useRef<{ upper: ISeriesApi<"Line"> | null; mid: ISeriesApi<"Line"> | null; lower: ISeriesApi<"Line"> | null }>({ upper:null, mid:null, lower:null });
  const volS    = useRef<ISeriesApi<"Histogram"> | null>(null);
  const rsiS    = useRef<{ line: ISeriesApi<"Line"> | null; ob: ISeriesApi<"Line"> | null; os: ISeriesApi<"Line"> | null }>({ line:null, ob:null, os:null });
  const macdS   = useRef<{ hist: ISeriesApi<"Histogram"> | null; line: ISeriesApi<"Line"> | null; sig: ISeriesApi<"Line"> | null }>({ hist:null, line:null, sig:null });
  const stochS  = useRef<{ k: ISeriesApi<"Line"> | null; d: ISeriesApi<"Line"> | null; ob: ISeriesApi<"Line"> | null; os: ISeriesApi<"Line"> | null }>({ k:null, d:null, ob:null, os:null });
  const atrS    = useRef<{ line: ISeriesApi<"Line"> | null }>({ line:null });
  const adxS    = useRef<{ adx: ISeriesApi<"Line"> | null; pdi: ISeriesApi<"Line"> | null; mdi: ISeriesApi<"Line"> | null; lvl: ISeriesApi<"Line"> | null }>({ adx:null, pdi:null, mdi:null, lvl:null });
  const customMASeriesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const allSubRef = useRef<IChartApi[]>([]);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const posLines = useRef<any[]>([]);

  const subDom: Record<SubKey, React.RefObject<HTMLDivElement | null>> = { rsi:rsiRef, macd:macdRef, stoch:stochRef, atr:atrRef, adx:adxRef };
  const subApi: Record<SubKey, React.MutableRefObject<IChartApi|null>> = { rsi:rsiChart, macd:macdChart, stoch:stochChart, atr:atrChart, adx:adxChart };

  useEffect(() => {
    if (!mainRef.current) return;
    const chart = createChart(mainRef.current, { ...CHART_OPTS, width: mainRef.current.clientWidth, height });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor:"#22c55e", downColor:"#ef4444",
      borderUpColor:"#22c55e", borderDownColor:"#ef4444",
      wickUpColor:"#22c55e", wickDownColor:"#ef4444",
    });
    mainChart.current = chart; candleS.current = candle;
    const allSub: IChartApi[] = [];
    if (rsiRef.current) {
      const sc = createChart(rsiRef.current, { ...CHART_OPTS, width:rsiRef.current.clientWidth, height:SUB_H.rsi });
      rsiChart.current = sc; allSub.push(sc);
      rsiS.current.line = sc.addSeries(LineSeries, { color:COLOR.rsi, lineWidth:1, priceLineVisible:false });
      rsiS.current.ob   = sc.addSeries(LineSeries, { color:"#ef4444aa", lineWidth:1, lineStyle:LineStyle.Dashed, priceLineVisible:false, lastValueVisible:false });
      rsiS.current.os   = sc.addSeries(LineSeries, { color:"#22c55eaa", lineWidth:1, lineStyle:LineStyle.Dashed, priceLineVisible:false, lastValueVisible:false });
    }
    if (macdRef.current) {
      const sc = createChart(macdRef.current, { ...CHART_OPTS, width:macdRef.current.clientWidth, height:SUB_H.macd });
      macdChart.current = sc; allSub.push(sc);
      macdS.current.hist = sc.addSeries(HistogramSeries, { priceLineVisible:false });
      macdS.current.line = sc.addSeries(LineSeries, { color:COLOR.macd, lineWidth:1, priceLineVisible:false });
      macdS.current.sig  = sc.addSeries(LineSeries, { color:"#f43f5e",   lineWidth:1, priceLineVisible:false });
    }
    if (stochRef.current) {
      const sc = createChart(stochRef.current, { ...CHART_OPTS, width:stochRef.current.clientWidth, height:SUB_H.stoch });
      stochChart.current = sc; allSub.push(sc);
      stochS.current.k  = sc.addSeries(LineSeries, { color:COLOR.stoch, lineWidth:1, priceLineVisible:false });
      stochS.current.d  = sc.addSeries(LineSeries, { color:"#94a3b8",   lineWidth:1, priceLineVisible:false });
      stochS.current.ob = sc.addSeries(LineSeries, { color:"#ef4444aa", lineWidth:1, lineStyle:LineStyle.Dashed, priceLineVisible:false, lastValueVisible:false });
      stochS.current.os = sc.addSeries(LineSeries, { color:"#22c55eaa", lineWidth:1, lineStyle:LineStyle.Dashed, priceLineVisible:false, lastValueVisible:false });
    }
    if (atrRef.current) {
      const sc = createChart(atrRef.current, { ...CHART_OPTS, width:atrRef.current.clientWidth, height:SUB_H.atr });
      atrChart.current = sc; allSub.push(sc);
      atrS.current.line = sc.addSeries(LineSeries, { color:COLOR.atr, lineWidth:1, priceLineVisible:false });
    }
    if (adxRef.current) {
      const sc = createChart(adxRef.current, { ...CHART_OPTS, width:adxRef.current.clientWidth, height:SUB_H.adx });
      adxChart.current = sc; allSub.push(sc);
      adxS.current.adx = sc.addSeries(LineSeries, { color:COLOR.adx,   lineWidth:2, priceLineVisible:false });
      adxS.current.pdi = sc.addSeries(LineSeries, { color:"#22c55e",   lineWidth:1, priceLineVisible:false });
      adxS.current.mdi = sc.addSeries(LineSeries, { color:"#ef4444",   lineWidth:1, priceLineVisible:false });
      adxS.current.lvl = sc.addSeries(LineSeries, { color:"#94a3b855", lineWidth:1, lineStyle:LineStyle.Dashed, priceLineVisible:false, lastValueVisible:false });
    }
    chart.timeScale().subscribeVisibleTimeRangeChange((range) => {
      if (range) allSub.forEach(sc => { try { sc.timeScale().setVisibleRange(range); } catch(_e) { /**/ } });
    });
    allSub.forEach(sc => {
      sc.timeScale().subscribeVisibleTimeRangeChange((range) => {
        if (range) { try { chart.timeScale().setVisibleRange(range); } catch(_e) { /**/ } }
      });
    });
    allSubRef.current = allSub;
    const ro = new ResizeObserver(() => {
      if (mainRef.current) chart.applyOptions({ width: mainRef.current.clientWidth });
      allSub.forEach((sc, i) => {
        const el = [rsiRef,macdRef,stochRef,atrRef,adxRef][i]?.current;
        if (el) sc.applyOptions({ width: el.clientWidth });
      });
    });
    ro.observe(mainRef.current);
    return () => {
      ro.disconnect(); chart.remove();
      allSub.forEach(sc => { try { sc.remove(); } catch(_e) { /**/ } });
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const chart = mainChart.current; const candle = candleS.current;
    if (!chart || !candle || !bars.length) return;
    const times  = bars.map(b => (new Date(b.time).getTime() / 1000) as Time);
    const closes = bars.map(b => b.close);
    const highs  = bars.map(b => b.high);
    const lows   = bars.map(b => b.low);
    candle.setData(bars.map((b, i) => ({ time:times[i], open:b.open, high:b.high, low:b.low, close:b.close })));
    const lineConfigs: { key: LineKey; vals: (number|null)[]; style?: number }[] = [
      { key:"ema8",   vals:_ema(closes, 8) },
      { key:"ema20",  vals:_ema(closes, 20) },
      { key:"ema21",  vals:_ema(closes, 21) },
      { key:"ema50",  vals:_ema(closes, 50) },
      { key:"ema200", vals:_ema(closes, 200) },
      { key:"sma20",  vals:_sma(closes, 20), style:LineStyle.Dashed },
      { key:"vwap",   vals:_vwap(bars),      style:LineStyle.Dotted },
    ];
    for (const { key, vals, style } of lineConfigs) {
      if (ind[key]) {
        if (!lineS.current[key]) {
          lineS.current[key] = chart.addSeries(LineSeries, {
            color:COLOR[key], lineWidth:1, lineStyle:style ?? LineStyle.Solid,
            priceLineVisible:false, lastValueVisible:false,
          });
        }
        lineS.current[key]!.setData(mapV(vals, times));
      } else if (lineS.current[key]) {
        chart.removeSeries(lineS.current[key]!); lineS.current[key] = null;
      }
    }
    if (ind.bbands) {
      const { mid, upper, lower } = _bb(closes);
      if (!bbS.current.mid) {
        bbS.current.mid   = chart.addSeries(LineSeries, { color:"#64748b", lineWidth:1, lineStyle:LineStyle.Dashed, priceLineVisible:false, lastValueVisible:false });
        bbS.current.upper = chart.addSeries(LineSeries, { color:"#475569", lineWidth:1, priceLineVisible:false, lastValueVisible:false });
        bbS.current.lower = chart.addSeries(LineSeries, { color:"#475569", lineWidth:1, priceLineVisible:false, lastValueVisible:false });
      }
      bbS.current.mid!.setData(mapV(mid, times));
      bbS.current.upper!.setData(mapV(upper, times));
      bbS.current.lower!.setData(mapV(lower, times));
    } else {
      if (bbS.current.mid)   { chart.removeSeries(bbS.current.mid);   bbS.current.mid   = null; }
      if (bbS.current.upper) { chart.removeSeries(bbS.current.upper); bbS.current.upper = null; }
      if (bbS.current.lower) { chart.removeSeries(bbS.current.lower); bbS.current.lower = null; }
    }
    if (ind.volume) {
      if (!volS.current) {
        volS.current = chart.addSeries(HistogramSeries, { color:COLOR.volume, priceFormat:{type:"volume"}, priceScaleId:"volume" });
        chart.priceScale("volume").applyOptions({ scaleMargins:{ top:0.78, bottom:0 }, visible:false });
      }
      volS.current.setData(bars.map((b, i) => ({ time:times[i], value:b.volume, color:b.close>=b.open?"#16a34a44":"#dc262644" })));
    } else if (volS.current) {
      chart.removeSeries(volS.current); volS.current = null;
    }
    if (rsiS.current.line) {
      const rsiData = mapV(_rsi(closes), times);
      rsiS.current.line.setData(rsiData);
      if (rsiData.length) {
        rsiS.current.ob!.setData(rsiData.map(d => ({ time:d.time, value:70 })));
        rsiS.current.os!.setData(rsiData.map(d => ({ time:d.time, value:30 })));
      }
    }
    if (macdS.current.line) {
      const { macd: m, signal: s, histogram: hh } = _macd(closes);
      macdS.current.line.setData(mapV(m, times));
      macdS.current.sig!.setData(mapV(s, times));
      macdS.current.hist!.setData(
        hh.map((v, i) => v !== null ? { time:times[i], value:v, color:v>=0?"#22c55e77":"#ef444477" } : null)
           .filter(Boolean) as { time:Time; value:number; color:string }[]
      );
    }
    if (stochS.current.k) {
      const { k, d } = _stoch(highs, lows, closes);
      const kData = mapV(k, times);
      stochS.current.k!.setData(kData);
      stochS.current.d!.setData(mapV(d, times));
      if (kData.length) {
        stochS.current.ob!.setData(kData.map(p => ({ time:p.time, value:80 })));
        stochS.current.os!.setData(kData.map(p => ({ time:p.time, value:20 })));
      }
    }
    if (atrS.current.line) atrS.current.line.setData(mapV(_atr(highs, lows, closes), times));
    if (adxS.current.adx) {
      const { adx, pdi, mdi } = _adx(highs, lows, closes);
      const adxData = mapV(adx, times);
      adxS.current.adx!.setData(adxData);
      adxS.current.pdi!.setData(mapV(pdi, times));
      adxS.current.mdi!.setData(mapV(mdi, times));
      if (adxData.length) adxS.current.lvl!.setData(adxData.map(p => ({ time:p.time, value:25 })));
    }

    // Custom MAs
    const liveIds = new Set(customMAs.map(m => m.id));
    // Remove series for deleted custom MAs
    customMASeriesRef.current.forEach((s, id) => {
      if (!liveIds.has(id)) { chart.removeSeries(s); customMASeriesRef.current.delete(id); }
    });
    // Add / update remaining
    for (const ma of customMAs) {
      const vals = ma.type === "ema" ? _ema(closes, ma.period) : _sma(closes, ma.period);
      if (!customMASeriesRef.current.has(ma.id)) {
        const s = chart.addSeries(LineSeries, {
          color: ma.color, lineWidth: 1,
          lineStyle: ma.type === "sma" ? LineStyle.Dashed : LineStyle.Solid,
          priceLineVisible: false, lastValueVisible: false,
        });
        customMASeriesRef.current.set(ma.id, s);
      }
      customMASeriesRef.current.get(ma.id)!.setData(mapV(vals, times));
    }

    chart.timeScale().fitContent();
    // Sync sub-charts using actual time values so warmup-period bar-count differences don't cause misalignment
    const timeRange = chart.timeScale().getVisibleRange();
    if (timeRange) allSubRef.current.forEach(sc => { try { sc.timeScale().setVisibleRange(timeRange); } catch(_e) { /**/ } });
  }, [bars, ind, customMAs]);

  useEffect(() => {
    const candle = candleS.current;
    if (!candle) return;
    posLines.current.forEach(l => { try { candle.removePriceLine(l); } catch(_e) { /**/ } });
    posLines.current = [];
    if (!positions?.length) return;
    for (const pos of positions) {
      posLines.current.push(candle.createPriceLine({ price:pos.open_price, color:pos.type==="buy"?"#3b82f6":"#f97316", lineWidth:1, lineStyle:LineStyle.Solid, axisLabelVisible:true, title:`${pos.type.toUpperCase()} #${pos.ticket}` }));
      if (pos.sl > 0) posLines.current.push(candle.createPriceLine({ price:pos.sl, color:"#ef4444", lineWidth:1, lineStyle:LineStyle.Dashed, axisLabelVisible:true, title:"SL" }));
      if (pos.tp > 0) posLines.current.push(candle.createPriceLine({ price:pos.tp, color:"#22c55e", lineWidth:1, lineStyle:LineStyle.Dashed, axisLabelVisible:true, title:"TP" }));
    }
  }, [positions]);

  useEffect(() => {
    for (const key of SUB_KEYS) {
      const el = subDom[key].current; const sc = subApi[key].current;
      if (ind[key] && el && sc) sc.applyOptions({ width: el.clientWidth });
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ind]);

  return (
    <div className="flex flex-col gap-0">
      <div ref={mainRef} className="w-full rounded-xl overflow-hidden" style={{ height }} />

      {SUB_KEYS.map(key => (
        <div key={key} style={{ height: ind[key] ? SUB_H[key] + 28 : 0, overflow:"hidden", transition:"height 200ms ease" }}>
          <div className="flex items-center justify-between mt-2 mb-0.5 px-1">
            <span className="text-[10px] text-gray-600 font-semibold uppercase tracking-wider">{SUB_LABEL[key]}</span>
            <button onClick={() => onToggle(key)} className="text-[10px] text-gray-700 hover:text-white px-1 transition-colors">×</button>
          </div>
          <div ref={subDom[key]} className="w-full rounded overflow-hidden" style={{ height: SUB_H[key] }} />
        </div>
      ))}
    </div>
  );
}
