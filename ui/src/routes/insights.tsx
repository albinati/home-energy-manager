import { useEffect } from "preact/hooks";
import { useFetch } from "../lib/poll";
import { getRecommendations } from "../lib/endpoints";
import { usePeriod, setGranularity, periodDateRange, isCurrentPeriod } from "../lib/period";
import { PeriodNavigator } from "../components/shell/PeriodNavigator";
import { Spinner } from "../components/common/Spinner";
import { ActionList } from "../components/insights/simple/ActionList";
import { EarlierReviews, HouseTiles, WeekTiles } from "../components/insights/simple/Tiles";
import "../styles/insights-simple.css";
import "./insights.css";

// Dev-only fixture switch (`?mock=1`); eliminated from the production bundle.
const MOCK = import.meta.env.DEV && new URLSearchParams(location.search).get("mock") === "1";

// Insights (#879 PR 2): "O que fazer". On load it fetches ONLY
// /insights/recommendations (+ /whoami from the app shell); the detailed cards
// behind each "ver mais" fetch their own data when expanded, and ECharts stays
// in the lazy chunk until then.
export default function Insights() {
  const period = usePeriod();
  // Default view = the current week (the navigator still offers other ranges).
  useEffect(() => { if (period.gran !== "week") setGranularity("week"); }, []);
  const end = periodDateRange(period).end;
  const rec = useFetch(
    async () => {
      if (import.meta.env.DEV && MOCK) return (await import("../lib/mock/recommendations")).MOCK_RECOMMENDATIONS;
      return getRecommendations(end);
    },
    [end],
    { cacheKey: `rec:${end}`, immutable: !isCurrentPeriod(period), track: true },
  );
  const data = rec.data;

  return (
    <div class="page-padded insights is-page">
      <header class="insights-head">
        <h1>Insights</h1>
        <p class="muted">O que fazer, como está indo e o que o sistema aprendeu da casa.</p>
      </header>

      <PeriodNavigator variant="page" />

      {rec.loading && !data && <Spinner label="Carregando…" />}
      {rec.error && !data && <p class="insights-error">Não foi possível carregar: {rec.error.message}</p>}

      {data && (
        <div class={rec.loading ? "is-updating" : undefined}>
          <section class="is-section" aria-labelledby="is-h-todo">
            <h2 class="is-h" id="is-h-todo">O que fazer</h2>
            <ActionList items={data.items} onApplied={() => rec.refresh()} />
            <EarlierReviews />
          </section>

          <section class="is-section" aria-labelledby="is-h-week">
            <h2 class="is-h" id="is-h-week">Como está indo · semana {data.week.start.slice(5).split("-").reverse().join("/")}–{data.week.end.slice(5).split("-").reverse().join("/")}</h2>
            <WeekTiles ws={data.week_summary} />
          </section>

          <section class="is-section" aria-labelledby="is-h-house">
            <h2 class="is-h" id="is-h-house">A casa · o que o sistema aprendeu</h2>
            <HouseTiles house={data.house} />
          </section>
        </div>
      )}
    </div>
  );
}
