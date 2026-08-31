import { GripVerticalIcon } from "lucide-react";
import { Button } from "@/components";

/**
 * The overlay's drag handle.
 *
 * The window is undecorated, so this grip is the only thing the user can move
 * it by. Upstream gated `data-tauri-drag-region` on an active licence and
 * showed a purchase popover to everyone else, which left an unlicensed install
 * unable to move its own window. Kaleo sells no licence, so the handle simply
 * works.
 */
export const DragButton = () => {
  return (
    <Button
      variant="ghost"
      size="icon"
      className="-ml-[2px] w-fit"
      data-tauri-drag-region
      title="Drag to move the window"
    >
      <GripVerticalIcon className="h-4 w-4" />
    </Button>
  );
};
