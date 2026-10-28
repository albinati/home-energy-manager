// The detailed cards that sit behind each tile's "ver mais" (#879). This module is
// only ever imported dynamically (MoreBox), so ECharts and the heavy cards stay in
// a lazy chunk and are fetched on the first expand.
import { usePeriod } from "../../../lib/period";
import { FairCompareBlock } from "../FairCompareBlock";
import { LoadPatternCard } from "../LoadPatternCard";
import { LoadForecastAccuracyCard } from "../LoadForecastAccuracyCard";
import { SolarClearnessCard } from "../SolarClearnessCard";
import { CosyScorecardCard } from "../CosyScorecardCard";
import { SystemHealthCard } from "../SystemHealthCard";
import { LwtLearningCard } from "../LwtLearningCard";
import { IndoorHistoryCard } from "../IndoorHistoryCard";
import { TuningSuggestionsCard } from "../TuningSuggestionsCard";

export type DetailKey = "bill" | "energy" | "solar" | "battery" | "cooling" | "heating" | "tuning";

export default function DetailCards({ which }: { which: DetailKey }) {
  const period = usePeriod();
  switch (which) {
    case "bill": return <FairCompareBlock />;
    case "energy": return <><LoadPatternCard period={period} /><LoadForecastAccuracyCard period={period} /></>;
    case "solar": return <SolarClearnessCard />;
    case "battery": return <><CosyScorecardCard /><SystemHealthCard period={period} /></>;
    case "cooling": return <IndoorHistoryCard />;
    case "heating": return <LwtLearningCard />;
    case "tuning": return <TuningSuggestionsCard />;
  }
}
