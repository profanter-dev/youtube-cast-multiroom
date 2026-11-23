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

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Outline;
import android.graphics.RenderEffect;
import android.graphics.Shader;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.LruCache;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewOutlineProvider;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Random;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import de.badaix.snapcast.utils.Settings;

/**
 * Full-screen "what's playing" for the TV: artwork, title, artist, progress.
 * Opened by {@link NowPlaying} when music starts; closes itself a few seconds
 * after it stops, which drops the TV back to whatever was on before.
 * The remote controls playback: OK = pause/play, left/right = previous/next.
 */
public class NowPlayingActivity extends AppCompatActivity implements NowPlaying.Listener {
    private static final long CLOSE_AFTER_STOP_MS = 10_000;
    private static final long DRIFT_EVERY_MS = 60_000;   // nudge the layout: no burn-in

    private static volatile boolean visible = false;

    public static boolean isVisible() {
        return visible;
    }

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService images = Executors.newSingleThreadExecutor();
    private static final LruCache<String, Bitmap> cache = new LruCache<>(8);
    private final Random random = new Random();

    private ImageView bg, cover;
    private TextView label, title, artist, elapsed, duration, next;
    private ProgressBar progress;
    private View content;
    private NowPlaying.Status status;
    private String shownVideoId = null;

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            renderProgress();
            handler.postDelayed(this, 250);
        }
    };
    private final Runnable closeIfStopped = this::finish;
    private final Runnable drift = new Runnable() {
        @Override
        public void run() {
            float d = getResources().getDisplayMetrics().density;
            content.animate()
                    .translationX((random.nextFloat() * 2 - 1) * 14 * d)
                    .translationY((random.nextFloat() * 2 - 1) * 10 * d)
                    .setDuration(4000).start();
            handler.postDelayed(this, DRIFT_EVERY_MS);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_now_playing);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        bg = findViewById(R.id.np_bg);
        cover = findViewById(R.id.np_cover);
        label = findViewById(R.id.np_label);
        title = findViewById(R.id.np_title);
        artist = findViewById(R.id.np_artist);
        elapsed = findViewById(R.id.np_elapsed);
        duration = findViewById(R.id.np_duration);
        next = findViewById(R.id.np_next);
        progress = findViewById(R.id.np_progress);
        content = findViewById(R.id.np_content);

        final float radius = 14 * getResources().getDisplayMetrics().density;
        cover.setOutlineProvider(new ViewOutlineProvider() {
            @Override
            public void getOutline(View view, Outline outline) {
                outline.setRoundRect(0, 0, view.getWidth(), view.getHeight(), radius);
            }
        });
        cover.setClipToOutline(true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S)
            bg.setRenderEffect(RenderEffect.createBlurEffect(60, 60, Shader.TileMode.CLAMP));
        hideSystemBars();
    }

    @Override
    protected void onStart() {
        super.onStart();
        visible = true;
        NowPlaying np = NowPlaying.get(this);
        np.acquire();                  // keep polling even if the service isn't running
        np.addListener(this);
        if (Settings.getInstance(this).getMusicbridgeUrl().isEmpty())
            showMessage(getString(R.string.np_not_configured));
        handler.post(tick);
        handler.postDelayed(drift, DRIFT_EVERY_MS);
    }

    @Override
    protected void onStop() {
        visible = false;
        NowPlaying np = NowPlaying.get(this);
        np.removeListener(this);
        np.release();
        handler.removeCallbacksAndMessages(null);
        super.onStop();
        // Out of sight (Home, another app): don't linger in the background —
        // the next song start opens a fresh one.
        if (!isChangingConfigurations())
            finish();
    }

    @Override
    protected void onDestroy() {
        images.shutdownNow();
        super.onDestroy();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus)
            hideSystemBars();
    }

    private void hideSystemBars() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
        }
    }

    // ------------------------------------------------------------- status ---

    @Override
    public void onStatus(NowPlaying.Status s) {
        if (s == null) {                       // unreachable: keep the last good screen
            if (status == null)
                showMessage(getString(R.string.np_unreachable));
            return;
        }
        status = s;
        if (s.current == null) {
            label.setText(R.string.np_idle);
            handler.removeCallbacks(closeIfStopped);
            handler.postDelayed(closeIfStopped, CLOSE_AFTER_STOP_MS);
            return;
        }
        handler.removeCallbacks(closeIfStopped);
        label.setText(s.loading ? R.string.np_loading : s.paused ? R.string.np_paused : R.string.np_now_playing);
        title.setText(s.current.title);
        artist.setText(s.current.artist);
        duration.setText(s.current.duration > 0 ? fmt(s.current.duration) : "");
        next.setText(s.next == null ? "" : getString(R.string.np_next,
                s.next.title + (s.next.artist.isEmpty() ? "" : " — " + s.next.artist)));
        if (!s.current.videoId.equals(shownVideoId)) {
            shownVideoId = s.current.videoId;
            loadImage(big(s.current.thumb, 800), cover);
            loadImage(s.current.thumb, bg);    // small on purpose: blurs when stretched
        }
        renderProgress();
    }

    private void showMessage(String msg) {
        label.setText("");
        title.setText(msg);
        artist.setText("");
        next.setText("");
    }

    private void renderProgress() {
        NowPlaying.Status s = status;
        if (s == null || s.current == null) {
            progress.setProgress(0);
            elapsed.setText("");
            return;
        }
        double e = s.elapsedNow();
        elapsed.setText(fmt(e));
        progress.setProgress(s.current.duration > 0 ? (int) (1000 * e / s.current.duration) : 0);
    }

    private static String fmt(double secs) {
        int s = (int) Math.max(0, secs);
        return (s / 60) + ":" + String.format(java.util.Locale.ROOT, "%02d", s % 60);
    }

    /** YouTube Music thumbnail URLs carry a size suffix (=w60-h60…); ask for a sharp one. */
    private static String big(String url, int px) {
        return url == null ? "" : url.replaceAll("=w\\d+-h\\d+", "=w" + px + "-h" + px);
    }

    private void loadImage(String url, ImageView into) {
        into.setTag(url);                       // the latest request wins
        if (url == null || url.isEmpty()) {
            into.setImageDrawable(null);
            return;
        }
        Bitmap cached = cache.get(url);
        if (cached != null) {
            into.setImageBitmap(cached);
            return;
        }
        images.execute(() -> {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(5000);
                c.setReadTimeout(8000);
                try (InputStream in = c.getInputStream()) {
                    Bitmap b = BitmapFactory.decodeStream(in);
                    if (b == null)
                        return;
                    cache.put(url, b);
                    handler.post(() -> {
                        if (url.equals(into.getTag()))   // track didn't change meanwhile
                            into.setImageBitmap(b);
                    });
                } finally {
                    c.disconnect();
                }
            } catch (Exception ignored) {
            }
        });
    }

    // ------------------------------------------------------------- remote ---

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        NowPlaying np = NowPlaying.get(this);
        NowPlaying.Status s = status;
        switch (keyCode) {
            case KeyEvent.KEYCODE_DPAD_CENTER:
            case KeyEvent.KEYCODE_ENTER:
            case KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE:
                if (s != null && s.current != null)
                    np.command(s.paused ? "resume" : "pause");
                return true;
            case KeyEvent.KEYCODE_MEDIA_PLAY:
                np.command("resume");
                return true;
            case KeyEvent.KEYCODE_MEDIA_PAUSE:
                np.command("pause");
                return true;
            case KeyEvent.KEYCODE_DPAD_RIGHT:
            case KeyEvent.KEYCODE_MEDIA_NEXT:
                np.command("next");
                return true;
            case KeyEvent.KEYCODE_DPAD_LEFT:
            case KeyEvent.KEYCODE_MEDIA_PREVIOUS:
                np.command("prev");
                return true;
        }
        return super.onKeyDown(keyCode, event);
    }
}
