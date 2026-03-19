"use client";
import { useEffect, useRef, useState } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickData,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  Time,
  ColorType,
  CrosshairMode,
} from "lightweight-charts";
import type { OHLCVBar } from "@/types";

interface Props {
  bars: OHLCVBar[];
  height?: number;
}

// ── Indicator math ──────────────────────────────────────────────────────────
function calcEMA(closes: number[], period: number): (number | null)[] {
  const k = 2 / (period + 1);
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period) return out;
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out[period - 1] = ema;
  for (let i = period; i < closes.length; i++) {
    ema = closes[i] * k + ema * (1 - k);
    out[i] = ema;
  }
  return out;
}

function calcRSI(closes: number[], period = 14): (number | null)[] {
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return out;
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) avgGain += d; else avgLoss += Math.abs(d);
  }
  avgGain /= period; avgLoss /= period;
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const g = d > 0 ? d : 0, l = d < 0 ? Math.abs(d) : 0;
    avgGain = (avgGain * (period - 1) + g) / period;
    avgLoss = (avgLoss * (period - 1) + l) / period;
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return out;
}

type IndicatorKey = "ema20" | "ema50" | "ema200" | "volume" | "rsi";
const INDICATOR_LABELS: Record<IndicatorKey, string> = {
  ema20: "EMA 20", ema50: "EMA 50", ema200: "EMA 200", volume: "Volume", rsi: "RSI 14",
};
const INDICATOR_COLORS: Record<IndicatorKey, string> = {
  ema20: "#f59e0b", ema50: "#3b82f6", ema200: "#a855f7", volume: "#334155", rsi: "#22d3ee",
};

const CHART_OPTIONS = {
  layout: { background: { type: ColorType.Solid, color: "#0f172a" }, textColor: "#94a3b8", attributionLogo: false },
  grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
  crosshair: { mode: CrosshairMode.Normal },
  rightPriceScale: { borderColor: "#1e293b" },
  timeScale: { borderColor: "#1e293b", timeVisible: true, secondsVisible: false },
} as const;

export default function TradingChart({ bars, height = 420 }: Props) {
  const mainRef = useRef<HTMLDivElement>(null);
  const rsiRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const rsiChartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const emaRefs = useRef<Partial<Record<IndicatorKey, ISeriesApi<"Line">>>>({});
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const rsiLineRef = useRef<ISeriesApi<"Line"> | null>(null);
  const rsiOb = useRef<ISeriesApi<"Line"> | null>(null);
  const rsiOs = useRef<ISeriesApi<"Line"> | null>(null);

  const [indicators, setIndicators] = useState<Record<IndicatorKey, boolean>>({
    ema20: true, ema50: true, ema200: false, volume: true, rsi: false,
  });

  const toggle = (k: IndicatorKey) =>
    setIndicators((prev) => ({ ...prev, [k]: !prev[k] }));

  // ── build / destroy charts ────────────────────────────────────────────────
  useEffect(() => {
    if (!mainRef.current) return;
    const chart = createChart(mainRef.current, {
      ...CHART_OPTIONS,
      width: mainRef.current.clientWidth,
      height,
    });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e", downColor: "#ef4444",
      borderUpColor: "#22c55e", borderDownColor: "#ef4444",
      wickUpColor: "#22c55e", wickDownColor: "#ef4444",
    });
    chartRef.current = chart;
    candleRef.current = candle;

    // RSI sub-chart
    if (rsiRef.current) {
      const rsiChart = createChart(rsiRef.current, {
        ...CHART_OPTIONS,
        width: rsiRef.current.clientWidth,
        height: 100,
      });
      rsiChartRef.current = rsiChart;
      rsiLineRef.current = rsiChart.addSeries(LineSeries, { color: INDICATOR_COLORS.rsi, lineWidth: 1, priceLineVisible: false });
      rsiOb.current  = rsiChart.addSeries(LineSeries, { color: "#ef4444", lineWidth: 1, lineStyle: 2, priceLineVisible: false });
      rsiOs.current  = rsiChart.addSeries(LineSeries, { color: "#22c55e", lineWidth: 1, lineStyle: 2, priceLineVisible: false });
      // Sync scroll
      chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (range) rsiChart.timeScale().setVisibleLogicalRange(range);
      });
      rsiChart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
        if (range) chart.timeScale().setVisibleLogicalRange(range);
      });
    }

    const ro = new ResizeObserver(() => {
      if (mainRef.current) chart.applyOptions({ width: mainRef.current.clientWidth });
      if (rsiRef.current && rsiChartRef.current) rsiChartRef.current.applyOptions({ width: rsiRef.current.clientWidth });
    });
    ro.observe(mainRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      rsiChartRef.current?.remove();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── update data whenever bars or indicator toggles change ─────────────────
  useEffect(() => {
    const chart = chartRef.current;
    const candle = candleRef.current;
    if (!chart || !candle || !bars.length) return;

    const times = bars.map((b) => (new Date(b.time).getTime() / 1000) as Time);
    const closes = bars.map((b) => b.close);

    // Candles
    const candleData: CandlestickData[] = bars.map((b, i) => ({
      time: times[i], open: b.open, high: b.high, low: b.low, close: b.close,
    }));
    candle.setData(candleData);

    // EMA indicators (Line series — add / remove as needed)
    const emaConfigs: { key: IndicatorKey; period: number }[] = [
      { key: "ema20", period: 20 },
      { key: "ema50", period: 50 },
      { key: "ema200", period: 200 },
    ];
    for (const { key, period } of emaConfigs) {
      if (indicators[key]) {
        if (!emaRefs.current[key]) {
          emaRefs.current[key] = chart.addSeries(LineSeries, {
            color: INDICATOR_COLORS[key], lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
          });
        }
        const emaVals = calcEMA(closes, period);
        const data = emaVals
          .map((v, i) => (v !== null ? { time: times[i], value: v } : null))
          .filter(Boolean) as { time: Time; value: number }[];
        emaRefs.current[key]!.setData(data);
      } else if (emaRefs.current[key]) {
        chart.removeSeries(emaRefs.current[key]!);
        delete emaRefs.current[key];
      }
    }

    // Volume
    if (indicators.volume) {
      if (!volRef.current) {
        volRef.current = chart.addSeries(HistogramSeries, {
          color: INDICATOR_COLORS.volume,
          priceFormat: { type: "volume" },
          priceScaleId: "volume",
        });
        chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.75, bottom: 0 } });
      }
      volRef.current.setData(
        bars.map((b, i) => ({
          time: times[i],
          value: b.volume,
          color: b.close >= b.open ? "#16a34a44" : "#dc262644",
        }))
      );
    } else if (volRef.current) {
      chart.removeSeries(volRef.current);
      volRef.current = null;
    }

    // RSI
    if (indicators.rsi && rsiLineRef.current && rsiOb.current && rsiOs.current) {
      const rsiVals = calcRSI(closes, 14);
      const rsiData = rsiVals
        .map((v, i) => (v !== null ? { time: times[i], value: v } : null))
        .filter(Boolean) as { time: Time; value: number }[];
      rsiLineRef.current.setData(rsiData);
      if (rsiData.length) {
        rsiOb.current.setData(rsiData.map((d) => ({ time: d.time, value: 70 })));
        rsiOs.current.setData(rsiData.map((d) => ({ time: d.time, value: 30 })));
      }
    }

    chart.timeScale().fitContent();
  }, [bars, indicators]);

  const showRsi = indicators.rsi;

  return (
    <div className="flex flex-col gap-0">
      {/* Indicator toolbar */}
      <div className="flex flex-wrap gap-1.5 mb-2">
        {(Object.keys(INDICATOR_LABELS) as IndicatorKey[]).map((k) => (
          <button
            key={k}
            onClick={() => toggle(k)}
            className={`px-2 py-0.5 rounded text-xs font-medium transition-colors border ${
              indicators[k]
                ? "border-transparent text-white"
                : "border-gray-700 text-gray-500 hover:text-gray-300"
            }`}
            style={indicators[k] ? { backgroundColor: INDICATOR_COLORS[k] + "33", borderColor: INDICATOR_COLORS[k], color: INDICATOR_COLORS[k] } : {}}
          >
            {INDICATOR_LABELS[k]}
          </button>
        ))}
      </div>

      {/* Main candlestick chart */}
      <div ref={mainRef} className="w-full rounded-xl overflow-hidden" style={{ height }} />

      {/* RSI sub-chart */}
      {showRsi && (
        <div className="mt-1">
          <div className="text-xs text-gray-600 px-1 mb-0.5">RSI (14)</div>
          <div ref={rsiRef} className="w-full rounded overflow-hidden" style={{ height: 100 }} />
        </div>
      )}
    </div>
  );
}
