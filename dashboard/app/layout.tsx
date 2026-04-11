import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import Sidebar from "@/components/nav/Sidebar";
import NotificationBar from "@/components/notifications/NotificationBar";
import AppBootstrap from "@/components/AppBootstrap";
import LogConsole from "@/components/LogConsole";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "EVOTRADE-AI | XM Trading Dashboard",
  description: "Algorithmic trading dashboard for XM MetaTrader 5",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased bg-gray-950 h-screen flex flex-col overflow-hidden`}
      >
        <AppBootstrap />
        <NotificationBar />
        <div className="flex flex-1 min-h-0">
          <Sidebar />
          <main className="flex-1 overflow-y-auto">
            {children}
          </main>
        </div>
        <LogConsole />
      </body>
    </html>
  );
}
