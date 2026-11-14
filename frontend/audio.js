/* 音声の入出力（マイク → 16kHz PCM16、24kHz PCM16 → スピーカー） */
(function () {
  "use strict";

  const WORKLET = `
  class PcmRecorder extends AudioWorkletProcessor {
    constructor() { super(); this.buf = []; this.len = 0; this.ratio = sampleRate / 16000; this.pos = 0; }
    process(inputs) {
      const ch = inputs[0] && inputs[0][0];
      if (!ch) return true;
      // 線形補間で 16kHz に間引く
      const out = [];
      while (this.pos < ch.length) {
        const i = Math.floor(this.pos), f = this.pos - i;
        const a = ch[i], b = i + 1 < ch.length ? ch[i + 1] : a;
        out.push(a + (b - a) * f);
        this.pos += this.ratio;
      }
      this.pos -= ch.length;
      let level = 0;
      const pcm = new Int16Array(out.length);
      for (let k = 0; k < out.length; k++) {
        const s = Math.max(-1, Math.min(1, out[k]));
        pcm[k] = s < 0 ? s * 0x8000 : s * 0x7fff;
        level = Math.max(level, Math.abs(s));
      }
      this.buf.push(pcm); this.len += pcm.length;
      if (this.len >= 1600) { // 100ms ごとに送る
        const joined = new Int16Array(this.len); let o = 0;
        for (const p of this.buf) { joined.set(p, o); o += p.length; }
        this.port.postMessage({ pcm: joined.buffer, level }, [joined.buffer]);
        this.buf = []; this.len = 0;
      }
      return true;
    }
  }
  registerProcessor("pcm-recorder", PcmRecorder);`;

  class Mic {
    constructor(onChunk, onLevel) { this.onChunk = onChunk; this.onLevel = onLevel || (() => {}); }
    async start() {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      this.ctx = new AudioContext();
      const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
      await this.ctx.audioWorklet.addModule(url);
      this.src = this.ctx.createMediaStreamSource(this.stream);
      this.node = new AudioWorkletNode(this.ctx, "pcm-recorder");
      this.node.port.onmessage = (e) => { this.onChunk(e.data.pcm); this.onLevel(e.data.level); };
      this.src.connect(this.node);
    }
    stop() {
      try { this.src && this.src.disconnect(); } catch (_) {}
      try { this.node && this.node.disconnect(); } catch (_) {}
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      if (this.ctx) this.ctx.close();
      this.stream = this.ctx = this.src = this.node = null;
    }
  }

  class Player {
    constructor(onActivity) { this.onActivity = onActivity || (() => {}); this.sources = new Set(); }
    ensure() {
      if (!this.ctx) { this.ctx = new AudioContext({ sampleRate: 24000 }); this.t = 0; }
      if (this.ctx.state === "suspended") this.ctx.resume();
    }
    play(arrayBuffer) {
      this.ensure();
      const pcm = new Int16Array(arrayBuffer);
      const buf = this.ctx.createBuffer(1, pcm.length, 24000);
      const data = buf.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 0x8000;
      const src = this.ctx.createBufferSource();
      src.buffer = buf; src.connect(this.ctx.destination);
      const now = this.ctx.currentTime;
      this.t = Math.max(this.t, now + 0.04);
      src.start(this.t);
      this.t += buf.duration;
      this.sources.add(src);
      this.onActivity(true);
      src.onended = () => {
        this.sources.delete(src);
        if (this.sources.size === 0) this.onActivity(false);
      };
    }
    stopAll() {
      for (const s of this.sources) { try { s.stop(); } catch (_) {} }
      this.sources.clear();
      if (this.ctx) this.t = this.ctx.currentTime;
      this.onActivity(false);
    }
    close() { this.stopAll(); if (this.ctx) this.ctx.close(); this.ctx = null; }
  }

  window.MinatoyaAudio = { Mic, Player };
})();
