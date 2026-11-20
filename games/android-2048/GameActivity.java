package org.dshlanya.game2048;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.os.Bundle;
import android.view.Gravity;
import android.view.MotionEvent;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.Arrays;
import java.util.Random;

public final class GameActivity extends Activity {
    private final Random random = new Random();
    private final int[] board = new int[16];
    private final TextView[] tiles = new TextView[16];
    private int score, moves;
    private TextView summary;
    private SharedPreferences save;
    private float downX, downY;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        save = getSharedPreferences("board", MODE_PRIVATE);
        if (!restore()) newGame();
        ScrollView scroll = new ScrollView(this);
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        page.setPadding(dp(18), dp(24), dp(18), dp(18));
        page.setBackgroundColor(0xfff6f3ec);
        scroll.addView(page);
        TextView title = text(36);
        title.setText("2048");
        page.addView(title);
        summary = text(17);
        page.addView(summary);
        LinearLayout grid = new LinearLayout(this);
        grid.setOrientation(LinearLayout.VERTICAL);
        grid.setPadding(dp(4), dp(4), dp(4), dp(4));
        grid.setBackgroundColor(0xffa39787);
        int height = dp(Math.max(38, Math.min(83, (getResources().getConfiguration().screenWidthDp - 64) / 4)));
        for (int row = 0; row < 4; row++) {
            LinearLayout line = new LinearLayout(this);
            for (int col = 0; col < 4; col++) {
                TextView tile = text(24);
                tiles[row * 4 + col] = tile;
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, height, 1);
                lp.setMargins(dp(2), dp(2), dp(2), dp(2));
                line.addView(tile, lp);
            }
            grid.addView(line);
        }
        grid.setContentDescription("2048 board, swipe to move");
        grid.setOnTouchListener((v, event) -> {
            if (event.getActionMasked() == MotionEvent.ACTION_DOWN) {
                v.getParent().requestDisallowInterceptTouchEvent(true);
                downX = event.getX(); downY = event.getY();
                return true;
            }
            if (event.getActionMasked() == MotionEvent.ACTION_UP) {
                float x = event.getX() - downX, y = event.getY() - downY;
                if (Math.max(Math.abs(x), Math.abs(y)) >= dp(18)) {
                    play(Math.abs(x) >= Math.abs(y) ? (x < 0 ? 0 : 1) : (y < 0 ? 2 : 3));
                } else v.performClick();
            }
            return true;
        });
        page.addView(grid);
        LinearLayout controls = new LinearLayout(this);
        String[] arrows = {"←", "→", "↑", "↓"};
        String[] labels = {"Left", "Right", "Up", "Down"};
        for (int i = 0; i < arrows.length; i++) {
            final int direction = i;
            Button button = new Button(this);
            button.setText(arrows[i]);
            button.setContentDescription(labels[i]);
            button.setOnClickListener(v -> play(direction));
            controls.addView(button, new LinearLayout.LayoutParams(0, dp(52), 1));
        }
        page.addView(controls);
        Button restart = new Button(this);
        restart.setText("New game");
        restart.setOnClickListener(v -> { newGame(); render(); });
        page.addView(restart);
        setContentView(scroll);
        render();
    }

    private TextView text(int size) {
        TextView text = new TextView(this);
        text.setGravity(Gravity.CENTER);
        text.setTextColor(0xff40362c);
        text.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        text.setTextSize(size);
        text.setPadding(0, dp(7), 0, dp(7));
        return text;
    }

    private int dp(int n) { return Math.round(n * getResources().getDisplayMetrics().density); }

    private boolean restore() {
        try {
            String[] pieces = save.getString("tiles", "").split(",");
            if (pieces.length != 16) return false;
            boolean populated = false;
            for (int i = 0; i < 16; i++) {
                int tile = Integer.parseInt(pieces[i]);
                if (tile < 0 || tile == 1 || tile > 131072 || (tile & (tile - 1)) != 0) return false;
                board[i] = tile;
                populated |= tile > 0;
            }
            score = save.getInt("score", 0); moves = save.getInt("moves", 0);
            return populated && score >= 0 && moves >= 0;
        } catch (RuntimeException invalid) { return false; }
    }

    private void persist() {
        StringBuilder values = new StringBuilder();
        for (int v : board) values.append(v).append(',');
        save.edit().putString("tiles", values.toString()).putInt("score", score).putInt("moves", moves).apply();
    }

    private void newGame() { Arrays.fill(board, 0); score = moves = 0; spawn(); spawn(); persist(); }

    private void spawn() {
        int[] empty = new int[16]; int count = 0;
        for (int i = 0; i < 16; i++) if (board[i] == 0) empty[count++] = i;
        if (count > 0) board[empty[random.nextInt(count)]] = random.nextInt(10) == 0 ? 4 : 2;
    }

    private int position(int direction, int line, int cell) {
        int n = direction % 2 == 0 ? cell : 3 - cell;
        return direction < 2 ? line * 4 + n : n * 4 + line;
    }

    private void play(int direction) {
        int[] next = new int[16]; int gained = 0;
        for (int line = 0; line < 4; line++) {
            int[] compact = new int[4]; int count = 0, output = 0;
            for (int i = 0; i < 4; i++) {
                int value = board[position(direction, line, i)];
                if (value != 0) compact[count++] = value;
            }
            for (int i = 0; i < count; i++) {
                int value = compact[i];
                if (i + 1 < count && compact[i + 1] == value) { value *= 2; gained += value; i++; }
                next[position(direction, line, output++)] = value;
            }
        }
        if (Arrays.equals(board, next)) return;
        System.arraycopy(next, 0, board, 0, 16);
        score += gained; moves++;
        spawn(); persist(); render();
    }

    private void render() {
        int[] colors = {0xffd2c6b6, 0xffeee3d3, 0xfff5d491, 0xfff7bd75, 0xfff4a571,
            0xffe98b64, 0xfff5c85e, 0xfff3bd48, 0xffedaf43, 0xffe8a439, 0xffde9333, 0xffd28a2f};
        boolean over = true, won = false;
        for (int i = 0; i < 16; i++) {
            int v = board[i];
            if (v == 0 || (i % 4 < 3 && v == board[i + 1]) || (i < 12 && v == board[i + 4])) over = false;
            won |= v >= 2048;
            tiles[i].setText(v == 0 ? "" : Integer.toString(v));
            tiles[i].setContentDescription(v == 0 ? "Empty" : Integer.toString(v));
            tiles[i].setTextSize(v >= 1024 ? 18 : 24);
            tiles[i].setBackgroundColor(colors[Math.min(colors.length - 1, v == 0 ? 0 : Integer.numberOfTrailingZeros(v))]);
        }
        summary.setText("Score " + score + "   ·   Moves " + moves + (over ? "\nNo moves left" : won ? "\n2048 reached!" : "\nSwipe the board to play"));
    }
}
