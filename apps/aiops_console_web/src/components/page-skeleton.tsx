import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

export function ListSkeleton({rows = 6, className}: {rows?: number; className?: string}) {
  return (
    <div className={cn("mx-auto w-full max-w-[1500px] px-4 py-6 lg:px-6", className)} role="status" aria-label="正在加载">
      <Skeleton className="h-7 w-40" />
      <div className="mt-6 grid gap-4">
        {Array.from({length: rows}, (_, index) => (
          <div key={index} className="flex items-center gap-4">
            <Skeleton className="h-4 flex-1" />
            <Skeleton className="h-5 w-24 rounded-full" />
          </div>
        ))}
      </div>
    </div>
  )
}

export function DetailSkeleton({className}: {className?: string}) {
  return (
    <div className={cn("mx-auto w-full max-w-[1500px] px-4 py-6 lg:px-6", className)} role="status" aria-label="正在加载">
      <Skeleton className="h-4 w-32" />
      <Skeleton className="mt-3 h-8 w-2/3" />
      <div className="mt-6 grid gap-4 lg:grid-cols-[300px_minmax(0,1fr)]">
        <Skeleton className="h-64 rounded-xl" />
        <div className="grid content-start gap-4">
          <Skeleton className="h-40 rounded-xl" />
          <Skeleton className="h-40 rounded-xl" />
        </div>
      </div>
    </div>
  )
}
