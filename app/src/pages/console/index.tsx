import { Header } from "@/components";
import { DebugConsole } from "@/components/debug";

const Console = () => (
  <div className="flex h-full min-h-0 flex-col gap-2">
    <Header
      isMainTitle
      title="Console"
      description="What actually happened, scrubbed of credentials — exportable for bug reports."
    />
    <DebugConsole className="min-h-0 flex-1" />
  </div>
);

export default Console;
