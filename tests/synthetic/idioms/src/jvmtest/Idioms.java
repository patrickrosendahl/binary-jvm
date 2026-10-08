package jvmtest;

import java.util.concurrent.TimeUnit;

/** Fixtures for javac's Java 8 desugaring (jvm-44): access$NNN accessors and the $SwitchMap$ of an enum switch. */
public class Idioms {
    private int i;
    private static int s;

    private void priv(int a) {
    }

    class Inner {
        int get() { return i; }
        void set(int v) { i = v; }
        void inc() { i++; }
        void pre() { --s; }
        void or() { i |= 2; }
        void call() { priv(1); }
        int stat() { return s; }
    }

    int sw(TimeUnit t) {
        switch (t) {
            case SECONDS: return 1;
            case MINUTES: return 2;
            default: return 0;
        }
    }
}
