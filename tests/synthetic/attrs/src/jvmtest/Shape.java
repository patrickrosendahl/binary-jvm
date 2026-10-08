package jvmtest;

public sealed class Shape permits Circle {
    public int k() {
        return 1;
    }
}
