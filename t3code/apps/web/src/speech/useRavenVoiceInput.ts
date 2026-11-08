import type { VoiceInputState } from "@t3tools/client-runtime/voice-input";
import { useEffect, useRef, useState } from "react";

import { useStableCallback } from "~/hooks/useStableCallback";
import workletUrl from "./pcmWorklet.ts?worker&url";

const IDLE_STATE: VoiceInputState = { phase: "idle", error: null, errorAction: null };
// Gladia sends its last finals after stop_recording and then closes; this bounds the wait.
const FINAL_TRANSCRIPT_TIMEOUT_MS = 5_000;

/** raven-core mints the Gladia live session, so the Gladia key never reaches the client. */
function ravenUrl(): string {
  const override = globalThis.localStorage?.getItem("raven:url")?.trim();
  return (override || import.meta.env.VITE_RAVEN_URL?.trim() || "http://localhost:8787").replace(
    /\/+$/,
    "",
  );
}

type GladiaMessage = {
  readonly type?: string;
  readonly data?: {
    readonly is_final?: boolean;
    readonly utterance?: { readonly text?: string };
  };
};

interface Recording {
  readonly token: number;
  readonly base: string;
  socket?: WebSocket;
  stream?: MediaStream | undefined;
  context?: AudioContext | undefined;
  node?: AudioWorkletNode | undefined;
  stopping: boolean;
}

function toInt16Pcm(pcm: Float32Array): ArrayBuffer {
  const out = new Int16Array(pcm.length);
  for (let index = 0; index < pcm.length; index++) {
    const sample = Math.max(-1, Math.min(1, pcm[index]!));
    out[index] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return out.buffer;
}

function joinTranscript(parts: ReadonlyArray<string>): string {
  return parts
    .map((part) => part.trim())
    .filter(Boolean)
    .join(" ");
}

function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return "Microphone access was denied.";
  }
  return `Voice input unavailable: ${error instanceof Error ? error.message : String(error)}`;
}

/**
 * Streams microphone PCM (16 kHz mono s16) to a Gladia live session that raven-core opens,
 * writing partial and final transcripts into the draft as they arrive.
 */
export function useRavenVoiceInput(input: {
  readonly disabled: boolean;
  readonly readDraft: () => string;
  readonly commitDraft: (text: string) => void;
}) {
  const [state, setState] = useState<VoiceInputState>(IDLE_STATE);
  const [level, setLevel] = useState(0);
  const latest = useRef(input);
  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- publishes committed draft callbacks to socket and worklet handlers
  useEffect(() => {
    latest.current = input;
  });
  const recordingRef = useRef<Recording | null>(null);
  const tokenRef = useRef(0);

  const release = (recording: Recording) => {
    if (recording.node) {
      recording.node.port.onmessage = null;
      recording.node.disconnect();
    }
    recording.stream?.getTracks().forEach((track) => track.stop());
    void recording.context?.close().catch(() => undefined);
    recording.node = undefined;
    recording.stream = undefined;
    recording.context = undefined;
    setLevel(0);
  };

  const end = (token: number, next: VoiceInputState) => {
    const recording = recordingRef.current;
    if (!recording || recording.token !== token) return;
    recordingRef.current = null;
    release(recording);
    if (recording.socket && recording.socket.readyState <= WebSocket.OPEN) recording.socket.close();
    setState(next);
  };

  const fail = (token: number, message: string) =>
    end(token, { phase: "error", error: message, errorAction: "retry" });

  const start = useStableCallback(async () => {
    if (latest.current.disabled || recordingRef.current) return;
    const token = ++tokenRef.current;
    const recording: Recording = { token, base: latest.current.readDraft(), stopping: false };
    recordingRef.current = recording;
    setState({ phase: "preparing", error: null, errorAction: null });
    const finals: string[] = [];
    const isCurrent = () => recordingRef.current === recording;
    try {
      const authToken = import.meta.env.VITE_RAVEN_TOKEN?.trim();
      const response = await fetch(`${ravenUrl()}/voice/session`, {
        method: "POST",
        headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
      });
      if (!response.ok) throw new Error(`raven-core answered ${response.status}`);
      const voice = (await response.json()) as { readonly url?: unknown };
      if (typeof voice.url !== "string") throw new Error("raven-core returned no session URL");
      if (!isCurrent()) return;

      recording.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      if (!isCurrent()) return release(recording);
      const context = new AudioContext();
      recording.context = context;
      await context.audioWorklet.addModule(workletUrl);
      if (!isCurrent()) return release(recording);
      const node = new AudioWorkletNode(context, "t3-pcm-capture", { channelCount: 1 });
      recording.node = node;

      const socket = new WebSocket(voice.url);
      socket.binaryType = "arraybuffer";
      recording.socket = socket;
      const queued: ArrayBuffer[] = [];
      socket.addEventListener("open", () => {
        for (const frame of queued.splice(0)) socket.send(frame);
      });
      socket.addEventListener("message", ({ data }: MessageEvent<unknown>) => {
        if (typeof data !== "string" || !isCurrent()) return;
        let message: GladiaMessage;
        try {
          message = JSON.parse(data) as GladiaMessage;
        } catch {
          return;
        }
        if (message.type !== "transcript") return;
        const text = message.data?.utterance?.text ?? "";
        if (message.data?.is_final) finals.push(text);
        latest.current.commitDraft(
          joinTranscript([recording.base, ...finals, message.data?.is_final ? "" : text]),
        );
      });
      socket.addEventListener("error", () => fail(token, "The Gladia connection failed."));
      socket.addEventListener("close", () => {
        if (recording.stopping) end(token, IDLE_STATE);
        else fail(token, "The Gladia session closed unexpectedly.");
      });

      node.port.onmessage = ({ data }: MessageEvent<unknown>) => {
        if (typeof data === "number") {
          setLevel(data);
        } else if (data instanceof Float32Array) {
          const frame = toInt16Pcm(data);
          if (socket.readyState === WebSocket.OPEN) socket.send(frame);
          else if (socket.readyState === WebSocket.CONNECTING) queued.push(frame);
        } else if (data === "limit") {
          void stop();
        } else if (data === "stopped") {
          // The worklet has flushed its last chunk, so the microphone can go.
          release(recording);
          if (socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ type: "stop_recording" }));
          } else {
            end(token, IDLE_STATE);
          }
        }
      };
      context.createMediaStreamSource(recording.stream).connect(node);
      // The processor emits silence, keeping the graph active without microphone playback.
      node.connect(context.destination);
      await context.resume();
      if (!isCurrent()) return release(recording);
      // oxlint-disable-next-line unicorn/require-post-message-target-origin -- AudioWorklet MessagePorts do not accept a target origin
      node.port.postMessage("start");
      setState({ phase: "recording", error: null, errorAction: null });
    } catch (error) {
      fail(token, errorMessage(error));
    }
  });

  const stop = useStableCallback(async () => {
    const recording = recordingRef.current;
    if (!recording || recording.stopping) return;
    if (!recording.node) return cancel();
    recording.stopping = true;
    setState({ phase: "transcribing", error: null, errorAction: null });
    // oxlint-disable-next-line unicorn/require-post-message-target-origin -- AudioWorklet MessagePorts do not accept a target origin
    recording.node.port.postMessage("stop");
    window.setTimeout(() => end(recording.token, IDLE_STATE), FINAL_TRANSCRIPT_TIMEOUT_MS);
  });

  const cancel = useStableCallback(() => {
    const recording = recordingRef.current;
    if (!recording) {
      setState(IDLE_STATE);
      return;
    }
    end(recording.token, IDLE_STATE);
    latest.current.commitDraft(recording.base);
  });

  // oxlint-disable-next-line t3code/no-unreasoned-state-sync -- releases the microphone and socket when the composer unmounts
  useEffect(
    () => () => {
      const recording = recordingRef.current;
      recordingRef.current = null;
      if (!recording) return;
      release(recording);
      recording.socket?.close();
    },
    [],
  );

  return {
    available:
      typeof navigator !== "undefined" &&
      Boolean(navigator.mediaDevices?.getUserMedia) &&
      typeof AudioWorkletNode !== "undefined",
    state,
    level,
    start,
    stop,
    cancel,
  };
}
