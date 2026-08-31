import {
  Settings,
  AudioLinesIcon,
  SquareSlashIcon,
  HomeIcon,
  CircuitBoardIcon,
  CableIcon,
  TerminalIcon,
  PowerIcon,
  BugIcon,
  GlobeIcon,
} from "lucide-react";
import { invoke } from "@tauri-apps/api/core";
import { GithubIcon } from "@/components";

/**
 * The dashboard sidebar.
 *
 * Only routes this app actually serves appear here. Upstream's chat, system
 * prompt, response, screenshot and dev-space entries went with the SaaS stack,
 * and so did the footer links that pointed at the upstream project's issue
 * tracker, marketing site, donation page and author's social account — a fork
 * must not route its users' bug reports or money to the project it forked.
 */
export const useMenuItems = () => {
  const menu: {
    icon: React.ElementType;
    label: string;
    href: string;
    count?: number;
  }[] = [
    {
      icon: CircuitBoardIcon,
      label: "Workbench",
      href: "/workbench",
    },
    {
      icon: CableIcon,
      label: "Engine",
      href: "/engine",
    },
    {
      icon: TerminalIcon,
      label: "Console",
      href: "/console",
    },
    {
      icon: HomeIcon,
      label: "Dashboard",
      href: "/dashboard",
    },
    {
      icon: Settings,
      label: "App Settings",
      href: "/settings",
    },
    {
      icon: AudioLinesIcon,
      label: "Audio",
      href: "/audio",
    },
    {
      icon: SquareSlashIcon,
      label: "Cursor & Shortcuts",
      href: "/shortcuts",
    },
  ];

  const footerItems = [
    {
      icon: BugIcon,
      label: "Report a bug",
      href: "https://github.com/machmoon/Kaleo/issues/new",
    },
    {
      icon: PowerIcon,
      label: "Quit Kaleo",
      action: async () => {
        await invoke("exit_app");
      },
    },
  ];

  const footerLinks: {
    title: string;
    icon: React.ElementType;
    link: string;
  }[] = [
    {
      title: "Github",
      icon: GithubIcon,
      link: "https://github.com/machmoon/Kaleo",
    },
    {
      title: "silkscreen",
      icon: GlobeIcon,
      link: "https://github.com/machmoon/silkscreen",
    },
  ];

  return {
    menu,
    footerItems,
    footerLinks,
  };
};
