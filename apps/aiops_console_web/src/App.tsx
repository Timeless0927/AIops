import { BrowserRouter, Navigate, Route, Routes } from "react-router"

import { TooltipProvider } from "@/components/ui/tooltip"
import { IncidentsPrototypePage } from "@/prototype/incidents-page"
import { WorkbenchPrototypePage } from "@/prototype/workbench-page"

export default function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Navigate to="/prototype/incidents" replace />} />
          <Route path="/prototype/incidents" element={<IncidentsPrototypePage />} />
          <Route
            path="/prototype/incidents/:incidentId"
            element={<WorkbenchPrototypePage />}
          />
          <Route path="*" element={<Navigate to="/prototype/incidents" replace />} />
        </Routes>
      </BrowserRouter>
    </TooltipProvider>
  )
}
