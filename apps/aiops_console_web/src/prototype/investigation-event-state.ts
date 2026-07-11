import type { InvestigationEvent } from "@/api/client"

export function appendInvestigationEvents(current: InvestigationEvent[], incoming: InvestigationEvent[]) {
  const seen = new Set(current.map((event) => event.id))
  return [...current, ...incoming.filter((event) => !seen.has(event.id))].sort((left, right) => left.id - right.id)
}
