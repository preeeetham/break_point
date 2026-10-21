import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { WorkflowProvider, useWorkflow } from "./lib/workflow";
import { useEvents } from "./lib/store";
import type { StageId } from "./lib/stages";
import { CameraHeader } from "./components/CameraHeader";
import { FeedDrawer } from "./components/Feed";
import { Container } from "./components/ui";
import { Lab } from "./acts/Lab";
import { LoadStation } from "./stations/LoadStation";
import { MapStation } from "./stations/MapStation";
import { PredictStation } from "./stations/PredictStation";
import { BaselineStation } from "./stations/BaselineStation";
import { PlanStation } from "./stations/PlanStation";
import { BreakStation } from "./stations/BreakStation";
import { ScoreStation } from "./stations/ScoreStation";
import { ReconcileStation } from "./stations/ReconcileStation";

const STATIONS: Record<StageId, () => React.JSX.Element> = {
  load: LoadStation,
  map: MapStation,
  predict: PredictStation,
  baseline: BaselineStation,
  plan: PlanStation,
  break: BreakStation,
  score: ScoreStation,
  reconcile: ReconcileStation,
};

export function App() {
  return (
    <MotionConfig reducedMotion="user">
      <WorkflowProvider>
        <a href="#workflow" className="sr-only-focusable fixed left-4 top-4 z-50 rounded-[2px] bg-fg px-4 py-2 text-bg">
          Skip to the workflow
        </a>
        <main>
          <Lab />
          <Camera />
        </main>
      </WorkflowProvider>
    </MotionConfig>
  );
}

function Camera() {
  const { stage } = useWorkflow();
  const events = useEvents();
  const Active = STATIONS[stage];
  return (
    <section id="workflow" data-theme="camera" aria-label="The workflow" className="relative flex min-h-svh flex-col bg-bg text-fg">
      <CameraHeader />
      <Container className="flex-1 py-10 md:py-14">
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={stage}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
          >
            <Active />
          </motion.div>
        </AnimatePresence>
      </Container>
      <div className="sticky bottom-0 z-30">
        <FeedDrawer events={events} />
      </div>
    </section>
  );
}
