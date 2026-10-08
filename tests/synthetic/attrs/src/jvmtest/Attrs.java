package jvmtest;

/** Fixtures for class-file attributes the Java 5 sample never emits (jvm-24). */
public class Attrs {
    public int abs(int x) {
        if (x > 0) {
            return x;
        }
        return -x;
    }

    public <T> T id(T value) {
        T local = value;
        return local;
    }

    public void named(String label) {
    }

    public class Inner {
    }

    public record Point(int x, int y) {
    }
}
