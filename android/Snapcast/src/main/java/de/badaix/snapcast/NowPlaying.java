/*
 *     This file is part of the Multiroom fork of Snapdroid.
 *
 *     This program is free software: you can redistribute it and/or modify
 *     it under the terms of the GNU General Public License as published by
 *     the Free Software Foundation, either version 3 of the License, or
 *     (at your option) any later version.
 *
 *     This program is distributed in the hope that it will be useful,
 *     but WITHOUT ANY WARRANTY; without even the implied warranty of
 *     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *     GNU General Public License for more details.
 *
 *     You should have received a copy of the GNU General Public License
 *     along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

package de.badaix.snapcast;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.PowerManager;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArraySet;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

import de.badaix.snapcast.utils.Settings;

/**
 * Polls musicbridge's /api/status and tells listeners what is playing. When
 * playback starts (nothing playing -> something playing) it opens the Now
 * Playing screen, if enabled. Runs while anyone holds it: the Snapclient
 * service (so it works in the background) and the Now Playing screen itself.
 */
public class NowPlaying {
    private static final String TAG = "NowPlaying";
    private static final long POLL_PLAYING_MS = 2000;
    private static final long POLL_IDLE_MS = 3000;

    public static class Track {
        public String videoId, title, artist, thumb;
        public double duration;

        static Track from(JSONObject o) {
            if (o == null)
                return null;
            Track t = new Track();
            t.videoId = o.optString("videoId", "");
            t.title = o.optString("title", "");
            t.artist = o.optString("artist", "");
            t.thumb = o.isNull("thumb") ? "" : o.optString("thumb", "");
            t.duration = o.optDouble("duration", 0);
            if (Double.isNaN(t.duration))
                t.duration = 0;
            return t;
        }
    }

    public static class Status {
        public Track current, next;
        public boolean paused, loading;
        public long playSeq = -1;       // musicbridge's "explicit play" counter; -1: not sent
        public double elapsed;          // seconds, as of receivedAt
        public long receivedAt;         // SystemClock.elapsedRealtime()

        /** Elapsed seconds now, extrapolated between polls. */
        public double elapsedNow() {
            double e = elapsed;
            if (!paused && !loading)
                e += (SystemClock.elapsedRealtime() - receivedAt) / 1000.0;
            if (current != null && current.duration > 0)
                e = Math.min(e, current.duration);
            return Math.max(0, e);
        }
    }

    public interface Listener {
        /** Called on the main thread. status == null: musicbridge unreachable. */
        void onStatus(Status status);
    }

    private static NowPlaying instance;

    public static synchronized NowPlaying get(Context context) {
        if (instance == null)
            instance = new NowPlaying(context.getApplicationContext());
        return instance;
    }

    private final Context ctx;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Set<Listener> listeners = new CopyOnWriteArraySet<>();
    private ScheduledExecutorService exec;
    private int holders = 0;
    private Status last;
    private boolean wasActive = false;  // playing and not paused, at the last poll
    private long lastSeq = -1;

    // TV switched on while music is already playing: there was no "started"
    // moment to catch, so open Now Playing when the screen comes on.
    private final BroadcastReceiver screenOn = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            main.postDelayed(() -> {
                if (last != null && last.current != null)
                    open("screen on during playback");
            }, 2500);                  // let the launcher settle first
        }
    };

    private NowPlaying(Context ctx) {
        this.ctx = ctx;
    }

    public synchronized void acquire() {
        if (holders++ == 0) {
            exec = Executors.newSingleThreadScheduledExecutor();
            exec.execute(this::pollAndReschedule);
            ctx.registerReceiver(screenOn, new IntentFilter(Intent.ACTION_SCREEN_ON));
        }
    }

    public synchronized void release() {
        if (holders > 0 && --holders == 0 && exec != null) {
            exec.shutdownNow();
            exec = null;
            try {
                ctx.unregisterReceiver(screenOn);
            } catch (IllegalArgumentException ignored) {
            }
        }
    }

    public void addListener(Listener l) {
        listeners.add(l);
        if (last != null)
            l.onStatus(last);
    }

    public void removeListener(Listener l) {
        listeners.remove(l);
    }

    public Status last() {
        return last;
    }

    /** Fire a transport action (pause, resume, next, prev) at musicbridge. */
    public void command(String action) {
        String base = Settings.getInstance(ctx).getMusicbridgeUrl();
        if (base.isEmpty())
            return;
        ScheduledExecutorService e;
        synchronized (this) {
            e = exec;
        }
        Runnable r = () -> {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(base + "/api/" + action).openConnection();
                c.setRequestMethod("POST");
                c.setConnectTimeout(4000);
                c.setReadTimeout(6000);
                c.setDoOutput(true);
                c.getOutputStream().close();
                Status s = parse(read(c));
                if (s != null)
                    deliver(s);
            } catch (Exception ex) {
                Log.w(TAG, "command " + action + " failed: " + ex);
            }
        };
        if (e != null)
            e.execute(r);
        else
            new Thread(r).start();
    }

    private void pollAndReschedule() {
        long delay = POLL_IDLE_MS;
        try {
            String base = Settings.getInstance(ctx).getMusicbridgeUrl();
            if (!base.isEmpty()) {
                HttpURLConnection c = (HttpURLConnection) new URL(base + "/api/status").openConnection();
                c.setConnectTimeout(4000);
                c.setReadTimeout(4000);
                Status s = parse(read(c));
                deliver(s);
                if (s != null && s.current != null)
                    delay = POLL_PLAYING_MS;
            }
        } catch (Exception e) {
            Log.d(TAG, "poll failed: " + e);
            deliver(null);
        }
        synchronized (this) {
            if (exec != null && !exec.isShutdown())
                exec.schedule(this::pollAndReschedule, delay, TimeUnit.MILLISECONDS);
        }
    }

    private static String read(HttpURLConnection c) throws Exception {
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0)
                out.write(buf, 0, n);
            return out.toString("UTF-8");
        } finally {
            c.disconnect();
        }
    }

    private static Status parse(String json) throws Exception {
        JSONObject o = new JSONObject(json);
        Status s = new Status();
        s.current = o.isNull("current") ? null : Track.from(o.optJSONObject("current"));
        s.paused = o.optBoolean("paused", false);
        s.loading = o.optBoolean("loading", false);
        s.elapsed = o.optDouble("elapsed", 0);
        s.playSeq = o.optLong("play_seq", -1);
        JSONArray q = o.optJSONArray("queue");
        s.next = (q != null && q.length() > 0) ? Track.from(q.optJSONObject(0)) : null;
        s.receivedAt = SystemClock.elapsedRealtime();
        return s;
    }

    private void deliver(Status s) {
        main.post(() -> {
            if (s != null) {
                // Open on: nothing/paused -> playing, or an explicit new "play"
                // (play_seq) while already playing. Not when the queue merely
                // advances, so a dismissed screen stays dismissed until then.
                boolean active = s.current != null && !s.paused;
                boolean replayed = s.playSeq >= 0 && lastSeq >= 0 && s.playSeq != lastSeq;
                if (active && (!wasActive || replayed))
                    open(wasActive ? "new playback started" : "playback started");
                wasActive = active;
                if (s.playSeq >= 0)
                    lastSeq = s.playSeq;
            }
            last = s;
            for (Listener l : listeners)
                l.onStatus(s);
        });
    }

    private void open(String why) {
        if (!Settings.getInstance(ctx).isNowPlayingPopup() || NowPlayingActivity.isVisible())
            return;
        PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        if (pm != null && !pm.isInteractive())
            return;                    // TV off/standby: never wake it; screenOn catches up
        Log.i(TAG, why + " — opening Now Playing");
        try {
            // Starting an activity from the background needs "Display over other
            // apps" (SYSTEM_ALERT_WINDOW) on Android 10+; see android/README.md.
            Intent i = new Intent(ctx, NowPlayingActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
            ctx.startActivity(i);
        } catch (Exception e) {
            Log.w(TAG, "could not open Now Playing: " + e);
        }
    }
}
