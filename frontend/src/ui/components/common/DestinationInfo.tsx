/**
 * DestinationInfo — an optional "About {city}" card enriched from Wikipedia.
 *
 * Fetches a short summary + lead image for the chosen destination and renders it with the
 * required "From Wikipedia" attribution (content is CC BY-SA). Enrichment is best-effort: while
 * loading, or when nothing is found, the component renders nothing, so the Results page is never
 * blocked on this network call. The loader is injectable for tests.
 */
import { useEffect, useState } from "react";

import {
  fetchDestinationSummary,
  type DestinationSummary,
} from "@/api/wikipedia";
import { IconArrowRight } from "@/ui/components/common/Icons";

interface DestinationInfoProps {
  city: string;
  load?: (city: string) => Promise<DestinationSummary | null>;
}

export default function DestinationInfo({
  city,
  load = fetchDestinationSummary,
}: DestinationInfoProps) {
  const [info, setInfo] = useState<DestinationSummary | null>(null);

  useEffect(() => {
    let active = true;
    setInfo(null);
    void load(city)
      .then((result) => {
        if (active) setInfo(result);
      })
      .catch(() => {
        /* best-effort: leave the section hidden */
      });
    return () => {
      active = false;
    };
  }, [city, load]);

  if (!info) return null;

  return (
    <section className="animate-fade overflow-hidden rounded-2xl bg-white shadow-sm ring-1 ring-slate-200">
      <div className="flex flex-col sm:flex-row">
        {info.imageUrl && (
          <img
            src={info.imageUrl}
            alt={`View of ${info.title}`}
            loading="lazy"
            className="h-44 w-full object-cover sm:h-auto sm:w-56 sm:shrink-0"
          />
        )}
        <div className="p-5">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-400">
            About {info.title}
          </h2>
          <p className="mt-2 line-clamp-4 text-sm leading-relaxed text-slate-700">
            {info.extract}
          </p>
          <a
            href={info.pageUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-teal-700 hover:underline"
          >
            From Wikipedia
            <IconArrowRight className="h-3.5 w-3.5" />
          </a>
        </div>
      </div>
    </section>
  );
}
